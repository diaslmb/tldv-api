import os
import re
import asyncio
import subprocess
from playwright.async_api import async_playwright, TimeoutError
import requests
import uuid
from summarizer import WorkflowAgentProcessor # <--- IMPORT

# --- (keep get_ffmpeg_command and transcribe_audio functions as they are) ---
# ...

async def run_bot_task(meeting_url: str, job_id: str, job_status: dict):
    # ... (keep the beginning of the function as it is) ...
    
    # --- (inside the run_bot_task function, the final block is modified) ---
    
    # --- (the finally block and browser.close() call remain the same) ---
            await browser.close()

    if os.path.exists(output_audio_path) and os.path.getsize(output_audio_path) > 1024:
        job_status[job_id] = {"status": "transcribing"}
        transcription_success = transcribe_audio(output_audio_path, output_transcript_path)
        
        if transcription_success:
            # --- NEW SUMMARIZATION STEP ---
            job_status[job_id] = {"status": "summarizing"}
            summarizer = WorkflowAgentProcessor(base_url="https://shai.pro/v1", api_key="app-GMysC0py6j6HQJsJSxI2Rbxb")
            
            file_id = await summarizer.upload_file(output_transcript_path)
            if file_id:
                summary_pdf_path = os.path.join(output_dir, "summary.pdf")
                summary_success = await summarizer.run_workflow(file_id, summary_pdf_path)
                if summary_success:
                    job_status[job_id] = {"status": "completed", "transcript_path": output_transcript_path, "summary_path": summary_pdf_path}
                else:
                    job_status[job_id] = {"status": "failed", "error": "Summarization failed."}
            else:
                job_status[job_id] = {"status": "failed", "error": "File upload for summarization failed."}
            # --- END OF NEW STEP ---
        else:
            job_status[job_id] = {"status": "failed", "error": "Transcription failed"}
    else:
        job_status[job_id] = {"status": "failed", "error": "Audio recording was empty or failed"}
