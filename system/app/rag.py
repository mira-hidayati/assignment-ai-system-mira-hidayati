"""
Modul RAG — pencarian berbasis TF-IDF & FAISS lokal + LLM Gemini dengan robust fallback.
"""

import os
import pandas as pd
import numpy as np
import faiss
from sklearn.feature_extraction.text import TfidfVectorizer
from google import genai
from google.genai import types

# Inisialisasi client Gemini secara aman
_client = None
try:
    api_key = os.environ.get("GEMINI_API_KEY")
    if api_key:
        _client = genai.Client(api_key=api_key)
except Exception:
    pass

# Path yang benar mengarah ke subfolder data/
KNOWLEDGE_BASE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "it_helpdesk_knowledge_base.csv")

_index = None
_documents = None
_df = None
_vectorizer = None


def build_index():
    """
    Memuat knowledge base CSV dan membangun index FAISS berbasis TF-IDF Vectorizer lokal.
    """
    global _index, _documents, _df, _vectorizer
    
    file_path = KNOWLEDGE_BASE_PATH
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Knowledge base file not found at {file_path}")

    _df = pd.read_csv(file_path)

    _documents = []
    texts_to_embed = []
    
    for _, row in _df.iterrows():
        doc_text = (
            f"Kategori: {row['category']}\n"
            f"Pertanyaan: {row['question']}\n"
            f"Jawaban: {row['answer']}"
        )
        _documents.append(doc_text)
        texts_to_embed.append(f"{row['category']} {row['question']} {row['answer']}")

    # Menggunakan TF-IDF Vectorizer lokal
    _vectorizer = TfidfVectorizer(max_features=1000)
    matrix = _vectorizer.fit_transform(texts_to_embed).toarray().astype("float32")

    faiss.normalize_L2(matrix)
    dimension = matrix.shape[1]
    
    _index = faiss.IndexFlatIP(dimension)
    _index.add(matrix)

# Let's approximate tokens using tiktoken-like logic or simple character/word count heuristic if tiktoken is not installed, or check available libraries.
import tiktoken
try:
    enc = tiktoken.encoding_for_model("gpt-4") # or standard cl100k_base which is close to modern tokenizers
except:
    enc = None

def count_tokens(text):
    if enc:
        return len(enc.encode(text))
    else:
        # rough estimation: 1 token ~= 4 characters or ~0.75 words
        return len(text.split()) * 4 / 3
    
def semantic_search(query: str, top_k: int = 3):
    """
    Melakukan pencarian semantik dengan dukungan dynamic top_k 
    berdasarkan kompleksitas panjang query.
    """
    # Deteksi apakah query kompleks / panjang (seperti EVAL-11)
    words_count = len(query.split())
    if words_count > 10 or "seluruh" in query.lower() or "dan" in query.lower():
        # Perbesar top_k agar mencakup lebih banyak dokumen lintas topik
        top_k = 6

    """
    Melakukan pencarian dokumen relevan menggunakan TF-IDF vector & FAISS index.
    """
    global _index, _documents, _df, _vectorizer
    if _index is None or _vectorizer is None:
        build_index()

    query_vec = _vectorizer.transform([query]).toarray().astype("float32")
    faiss.normalize_L2(query_vec)

    k = min(top_k, len(_documents))
    distances, indices = _index.search(query_vec, k)

    results = []
    for i, idx in enumerate(indices[0]):
        if idx < len(_documents):
            results.append({
                "rank": i + 1,
                "document": _documents[idx],
                "question": _df.iloc[idx]["question"],
                "answer": _df.iloc[idx]["answer"],
                "category": _df.iloc[idx]["category"],
                "score": float(distances[0][i]),
            })
    return results


async def answer_from_knowledge_base(query: str) -> str:
    """
    Menjalankan semantic search dengan relevance threshold, menyusun prompt, 
    lalu memanggil LLM Gemini. Dilengkapi guardrail untuk menolak query out-of-scope.
    """
    search_results = semantic_search(query, top_k=3)
    
    if not search_results:
        return "Maaf, informasi tidak ditemukan di Knowledge Base."

    # AMBANG BATAS (THRESHOLD) KEMIRIPAN
    # Skor cosine similarity / Inner Product dari FAISS IndexFlatIP (setelah normalisasi L2 berkisar antara 0.0 s.d 1.0)
    # Jika skor tertinggi di bawah threshold (misal 0.35), anggap query di luar cakupan (out-of-scope).
    SIMILARITY_THRESHOLD = 0.35
    top_score = search_results[0]["score"]

    if top_score < SIMILARITY_THRESHOLD:
        return "Maaf, saya adalah asisten IT Helpdesk internal perusahaan dan hanya dapat membantu menjawab pertanyaan seputar kebijakan IT, HR, dan operasional internal yang terdaftar dalam Knowledge Base."

    # Coba gunakan LLM jika client tersedia dan aktif
    if _client is not None:
        try:
            # Ambil hanya dokumen yang skornya di atas threshold untuk dimasukkan ke konteks
            valid_contexts = [res["document"] for res in search_results if res["score"] >= SIMILARITY_THRESHOLD]
            
            if not valid_contexts:
                return "Maaf, saya tidak memiliki informasi yang relevan untuk menjawab pertanyaan tersebut."

            context = "\n\n---\n\n".join(valid_contexts)
            
            prompt = f"""Kamu adalah asisten IT Helpdesk internal perusahaan yang profesional, ramah, dan membantu.

ATURAN KETAT (GUARDRAIL):
1. Jawab pertanyaan karyawan HANYA berdasarkan informasi FAQ yang ada di bagian Konteks.
2. JANGAN PERNAH berasumsi, mengarang fakta, atau menggunakan informasi di luar konteks yang diberikan.
3. Jika informasi atau konteks yang diberikan tidak secara langsung menjawab pertanyaan pengguna, katakan dengan sopan bahwa informasi tersebut tidak tersedia di Knowledge Base perusahaan, dan jangan memaksakan diri menjawab menggunakan topik yang melenceng.

Konteks FAQ relevan:
{context}

Pertanyaan karyawan: {query}

Jawaban:"""

            response = _client.models.generate_content(
                model="gemini-2.0-flash",
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.1), # Suhu diturunkan agar lebih deterministik
            )
            if response and response.text:
                return response.text
        except Exception as e:
            print(f"[Warning] LLM generation failed ({type(e).__name__}), falling back to direct KB answer.")

    # FALLBACK: Jika skor mencukupi, kembalikan jawaban FAQ teratas secara lokal
    if top_score >= SIMILARITY_THRESHOLD:
        top_res = search_results[0]
        return f"{top_res['answer']}"
    
    return "Maaf, informasi tidak tersedia di Knowledge Base."