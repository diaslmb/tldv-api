import os
import re
import asyncio
import subprocess
from playwright.async_api import async_playwright, TimeoutError
import requests
import uuid
from summarizer import WorkflowAgentProcessor

# --- CONFIGURATION ---
MAX_MEETING_DURATION_SECONDS = 10800
WHISPERX_URL = "http://localhost:8000/v1/audio/transcriptions"

def get_ffmpeg_command(platform, duration, output_path):
    if platform.startswith("linux"):
        return ["ffmpeg", "-y", "-f", "pulse", "-i", "default", "-t", str(duration), output_path]
    return None

def transcribe_audio(audio_path, transcript_path):
    if not os.path.exists(audio_path):
        print(f"❌ Audio file not found at {audio_path}")
        return False
    print(f"🎤 Sending {audio_path} to whisperx for transcription...")
    try:
        with open(audio_path, 'rb') as f:
            files = {'file': (os.path.basename(audio_path), f)}
            response = requests.post(WHISPERX_URL, files=files)
        if response.status_code == 200:
            transcript_data = response.json()
            clean_transcript = transcript_data.get('text', '').replace('<br>', '\n')
            with open(transcript_path, 'w') as f:
                f.write(clean_transcript)
            print(f"✅ Transcription successful. Saved to {transcript_path}")
            return True
        else:
            print(f"❌ Transcription failed. Status code: {response.status_code}\n{response.text}")
            return False
    except requests.exceptions.RequestException as e:
        print(f"❌ Error connecting to whisperx service: {e}")
        return False
    except Exception as e:
        print(f"An unexpected error occurred during transcription: {e}")
        return False

async def run_bot_task(meeting_url: str, job_id: str, job_status: dict):
    import sys
    
    output_dir = os.path.join("outputs", job_id)
    os.makedirs(output_dir, exist_ok=True)
    output_audio_path = os.path.join(output_dir, "meeting_audio.wav")
    output_transcript_path = os.path.join(output_dir, "transcript.txt")
    
    ffmpeg_command = get_ffmpeg_command(sys.platform, MAX_MEETING_DURATION_SECONDS, output_audio_path)
    if not ffmpeg_command:
        job_status[job_id] = {"status": "failed", "error": "Unsupported OS"}
        return

    job_status[job_id] = {"status": "starting_browser"}
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled", "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"])
            context = await browser.new_context(permissions=["microphone", "camera"])
            page = await context.new_page()
        except Exception as e:
            job_status[job_id] = {"status": "failed", "error": f"Failed to launch browser: {e}"}
            return
            
        recorder = None
        try:
            job_status[job_id] = {"status": "navigating"}
            await page.goto(meeting_url, timeout=60000)
            
            try:
                await page.get_by_role("button", name="Continue on this browser").click(timeout=15000)
            except TimeoutError:
                pass 
            
            name_input = page.get_by_placeholder("Type your name")
            await name_input.wait_for(state="visible", timeout=30000)
            await name_input.fill("SHAI AI Notetaker")
            
            try:
                await page.get_by_role("button", name="Microphone on", exact=True).click(timeout=5000)
            except TimeoutError:
                pass 

            try:
                await page.get_by_role("button", name="Camera on", exact=True).click(timeout=5000)
            except TimeoutError:
                pass

            join_button_locator = page.get_by_role("button", name="Join now")
            await join_button_locator.wait_for(timeout=15000)
            
            job_status[job_id] = {"status": "recording"}
            recorder = subprocess.Popen(ffmpeg_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            await join_button_locator.click(timeout=15000)

            await page.get_by_role("button", name=re.compile("Leave|Hang up", re.IGNORECASE)).wait_for(state="visible", timeout=45000)
            print("✅ Bot has successfully joined the meeting.")
            await asyncio.sleep(5) 

            while True:
                await asyncio.sleep(5) 
                
                try:
                    if job_status.get(job_id, {}).get("status") == "stopping":
                        print("Stop signal received, leaving meeting.")
                        break
                    
                    is_in_lobby = await page.locator('text*="waiting for others to join"').or_(page.locator('text*="Someone in the meeting should let you in soon"')).is_visible()
                    if is_in_lobby:
                        print("🕒 Bot is in the lobby, waiting...")
                        continue

                    participant_button = page.get_by_role("button", name=re.compile("People|Participants|Участники", re.IGNORECASE))
                    await participant_button.click()
                    
                    # --- NEW, MORE ROBUST SELECTOR ---
                    # This looks for any element designated as a "listitem" inside the side panel.
                    # This is much more reliable than looking for a specific list label.
                    participant_list_selector = 'div[role="complementary"] [role="listitem"]'
                    
                    await page.locator(participant_list_selector).first.wait_for(state="visible", timeout=15000) # Increased timeout
                    
                    participant_count = await page.locator(participant_list_selector).count()
                    print(f"👥 Found {participant_count} participant(s).")

                    if participant_count <= 1:
                        print("Only 1 participant left. Ending recording.")
                        break
                    
                    await participant_button.click() # Close the panel

                except (TimeoutError, AttributeError) as e:
                    print(f"❌ Could not find participant count: {e}. Ending recording.")
                    # Take a screenshot right when the error happens for debugging
                    await page.screenshot(path=os.path.join(output_dir, "participant_error.png"))
                    break
        except Exception as e:
            job_status[job_id] = {"status": "failed", "error": f"An error occurred in the meeting: {e}"}
            await page.screenshot(path=os.path.join(output_dir, "error.png"))
        finally:
            if recorder and recorder.poll() is None:
                recorder.terminate()
                recorder.communicate()
            
            try:
                await page.get_by_role("button", name=re.compile("Leave|Hang up", re.IGNORECASE)).click(timeout=5000)
                await asyncio.sleep(3)
            except Exception: pass
            
            await browser.close()

    if os.path.exists(output_audio_path) and os.path.getsize(output_audio_path) > 1024:
        job_status[job_id] = {"status": "transcribing"}
        transcription_success = transcribe_audio(output_audio_path, output_transcript_path)
        
        if transcription_success:
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
        else:
            job_status[job_id] = {"status": "failed", "error": "Transcription failed"}
    else:
        job_status[job_id] = {"status": "failed", "error": "Audio recording was empty or failed"}
