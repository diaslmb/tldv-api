import os
import httpx
from loguru import logger

# === CONFIG FOR YOUR WORKFLOW AGENT ===
API_KEY = "app-GMysC0py6j6HQJsJSxI2Rbxb"
BASE_URL = "https://shai.pro/v1"

class WorkflowAgentProcessor:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url
        self.api_key = api_key
        self.headers = {"Authorization": f"Bearer {self.api_key}"}

    async def run_workflow_with_text(self, transcript_path: str, output_pdf_path: str) -> bool:
        """Reads a text file and runs the workflow with its content."""
        url = f"{self.base_url}/workflows/run"
        
        try:
            with open(transcript_path, "r", encoding="utf-8") as f:
                transcript_content = f.read()
        except Exception as e:
            logger.error(f"Failed to read transcript file at {transcript_path}: {e}")
            return False

        # The payload now sends the transcript content directly to the "text" variable
        payload = {
            "user": "user",
            "response_mode": "blocking",
            "inputs": {
                "text": transcript_content
            }
        }

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
                async with client.stream("POST", url, headers=self.headers, json=payload) as response:
                    response.raise_for_status()
                    
                    if "application/pdf" in response.headers.get("content-type", ""):
                        with open(output_pdf_path, "wb") as f:
                            async for chunk in response.aiter_bytes():
                                f.write(chunk)
                        logger.success(f"Successfully saved PDF summary to {output_pdf_path}")
                        return True
                    else:
                        error_text = await response.aread()
                        logger.error(f"Workflow did not return a PDF. Response: {error_text.decode()}")
                        return False
        except Exception as e:
            logger.error(f"Workflow run failed: {e}")
            return False
