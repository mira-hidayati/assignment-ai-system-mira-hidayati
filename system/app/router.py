"""
Router — implementasi lengkap dengan robust fallback untuk intent detection.
"""

import os
import re
from google import genai
from google.genai import types

from app.rag import answer_from_knowledge_base
from app.tools import get_ticket_status

# Inisialisasi client secara aman
_client = None
try:
    api_key = os.environ.get("GEMINI_API_KEY")
    if api_key:
        _client = genai.Client(api_key=api_key)
except Exception:
    pass

TICKET_ID_PATTERN = re.compile(r"TICKET-\d{4}", re.IGNORECASE)


async def detect_intent(query: str) -> str:
    """
    Menentukan intent dari query user: 'faq' atau 'ticket_status'.
    Dilengkapi fallback berbasis keyword & pattern matching jika LLM gagal/ClientError.
    """
    if TICKET_ID_PATTERN.search(query):
        return "ticket_status"

    # Jika client tersedia, coba deteksi intent via LLM
    if _client is not None:
        try:
            prompt = f"""Klasifikasikan pertanyaan berikut ke dalam salah satu kategori:
'faq' (pertanyaan informasi/kebijakan umum seputar cuti, akses, perangkat, HR, dsb)
atau 'ticket_status' (permintaan mengecek status tiket tertentu).

Jawab HANYA dengan satu kata: faq atau ticket_status.

Pertanyaan: {query}"""

            response = _client.models.generate_content(
                model="gemini-2.0-flash",
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.0),
            )
            intent = response.text.strip().lower()
            if intent in ("faq", "ticket_status"):
                return intent
        except Exception:
            pass  # Fallback ke rule-based jika terjadi ClientError/kendala API

    # FALLBACK RULE-BASED: Deteksi intent berdasarkan keyword sederhana
    query_lower = query.lower()
    if any(kw in query_lower for kw in ["tiket", "ticket", "status"]):
        return "ticket_status"
    
    return "faq"


def extract_ticket_id(query: str) -> str:
    """Mengekstrak ticket_id dari teks query menggunakan regex."""
    match = TICKET_ID_PATTERN.search(query)
    if match:
        return match.group(0).upper()
    return ""


async def handle_query(query: str) -> dict:
    """Fungsi utama yang dipanggil oleh endpoint /ask dengan dukungan Hybrid/Mixed Intent."""
    try:
        query_lower = query.lower()
        
        # Deteksi apakah query mengandung unsur FAQ teknis DAN permintaan tiket sekaligus (Mixed Intent)
        has_ticket_keyword = any(kw in query_lower for kw in ["tiket", "ticket", "status", "cek tiket"])
        has_ticket_id = bool(TICKET_ID_PATTERN.search(query))
        has_faq_keyword = any(kw in query_lower for kw in ["kenapa", "bagaimana", "cara", "kebijakan", "lambat", "cuti", "password", "laptop"])

        # Jika mixed intent (menanyakan masalah teknis tapi menyinggung soal tiket tanpa ID spesifik)
        if has_faq_keyword and has_ticket_keyword and not has_ticket_id:
            # 1. Ambil jawaban dari Knowledge Base (RAG) untuk mengatasi pertanyaan teknis
            faq_response = await answer_from_knowledge_base(query)
            
            # 2. Gabungkan dengan instruksi klarifikasi tiket secara sopan
            combined_response = (
                f"{faq_response}\n\n"
                f"*(Catatan: Anda juga menanyakan perihal pengecekan tiket. Mohon sertakan ID tiket dengan format TICKET-XXXX jika ingin melacak status tiket terkait.)*"
            )
            return {
                "status": "success", 
                "intent": "hybrid", 
                "data": combined_response
            }

        # Jalur standar jika hanya murni ticket_status dengan ID valid
        intent = await detect_intent(query)

        if intent == "ticket_status":
            ticket_id = extract_ticket_id(query)
            if not ticket_id:
                return {
                    "status": "error",
                    "intent": "ticket_status",
                    "data": "Mohon sertakan ID tiket (format: TICKET-XXXX) pada pertanyaan Anda.",
                }
            data = get_ticket_status(ticket_id)
            return {"status": "success", "intent": "ticket_status", "data": data}

        else:  # intent == "faq"
            data = await answer_from_knowledge_base(query)
            return {"status": "success", "intent": "faq", "data": data}

    except Exception as e:
        return {
            "status": "error",
            "intent": "unknown",
            "data": f"Terjadi kesalahan saat memproses permintaan Anda. Silakan coba lagi. ({type(e).__name__})",
        }

async def route_query(query: str) -> dict:
    """
    Menentukan rute intent dari query pengguna.
    Mendukung single intent (faq / ticket_status) maupun multi-intent/hybrid.
    """
    query_lower = query.lower()
    
    # Deteksi keberadaan ID tiket atau pola regex tiket
    has_ticket_keyword = any(kw in query_lower for kw in ["tiket", "ticket", "status tiket", "cek tiket"])
    has_faq_keyword = any(kw in query_lower for kw in ["kenapa", "bagaimana", "cara", "kebijakan", "syarat", "lambat", "cuti", "password"])
    
    # Deteksi jika ada format TICKET-XXXX langsung
    import re
    has_ticket_id = bool(re.search(r'ticket-\d+', query_lower))

    # Jika mengandung indikasi cek tiket TETAPI juga menanyakan masalah teknis (Mixed Intent seperti EVAL-09)
    if has_ticket_keyword and has_faq_keyword:
        return {
            "intent": "hybrid",
            "actions": ["faq", "ticket_status"],
            "reason": "Query mencakup informasi FAQ dan permintaan penelusuran tiket sekaligus."
        }
    
    # Single Intent Standard
    if has_ticket_id or (has_ticket_keyword and not has_faq_keyword):
        return {
            "intent": "ticket_status",
            "actions": ["ticket_status"],
            "reason": "Query berfokus pada status atau pelacakan tiket."
        }
        
    return {
        "intent": "faq",
        "actions": ["faq"],
        "reason": "Query berfokus pada pencarian informasi FAQ pengetahuan internal."
    }