import os
import re
import asyncio
import subprocess
from playwright.async_api import async_playwright, TimeoutError
import requests
from summarizer import WorkflowAgentProcessor

# --- CONFIGURATION ---
MAX_MEETING_DURATION_SECONDS = 10800
WHISPERX_URL = "http://88.204.158.4:8080/v1/audio/transcriptions"

def get_ffmpeg_command(platform, duration, output_path):
    if platform.startswith("linux"):
        return ["ffmpeg", "-y", "-f", "pulse", "-i", "default", "-t", str(duration), output_path]
    return None

def extract_meeting_details(url: str):
    """Extracts meeting ID and password from a Zoom URL."""
    match = re.search(r'/j/(\d+)\?pwd=([\w.-]+)', url)
    if match:
        return match.group(1), match.group(2)
    return None, None

def transcribe_audio(audio_path, transcript_path):
    if not os.path.exists(audio_path):
        print(f"❌ Audio file not found at {audio_path}")
        return False
    print(f"🎤 Sending {audio_path} to whisperx for transcription...")
    try:
        with open(audio_path, 'rb') as f:
            files = {'file': (os.path.basename(audio_path), f)}
            data = {'model': 'whisper-large-v3'}
            response = requests.post(WHISPERX_URL, files=files, data=data)
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

    meeting_id, pwd = extract_meeting_details(meeting_url)
    if not meeting_id or not pwd:
        job_status[job_id] = {"status": "failed", "error": "Could not extract Meeting ID and/or Password from URL."}
        return

    job_status[job_id] = {"status": "starting_browser"}
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
            context = await browser.new_context(permissions=["microphone", "camera"])
            page = await context.new_page()
        except Exception as e:
            job_status[job_id] = {"status": "failed", "error": f"Failed to launch browser: {e}"}
            return

        recorder = None
        try:
            job_status[job_id] = {"status": "navigating"}
            await page.goto("https://app.zoom.us/wc/join", timeout=60000)
            print("✅ Navigated to join page.")

            meeting_id_placeholder = re.compile("Meeting ID|Идентификатор конференции", re.IGNORECASE)
            await page.get_by_placeholder(meeting_id_placeholder).fill(meeting_id)
            join_button_text = re.compile("Join|Подключиться", re.IGNORECASE)
            await page.get_by_role("button", name=join_button_text).click()
            print(f"✅ Entered Meeting ID: {meeting_id}")

            # The entire join form and meeting UI is in an iframe. Let's define it once.
            frame = page.frame_locator('iframe').first
            
            # Wait for an element inside the iframe to ensure it's loaded
            await frame.locator('body').wait_for(timeout=30000)
            print("✅ Pre-join iframe is ready.")

            # Mute and Stop Video on the pre-join screen
            try:
                mute_button = frame.get_by_role("button", name="Mute")
                await mute_button.wait_for(state="visible", timeout=10000)
                if await mute_button.is_enabled():
                    await mute_button.click()
                    print("✅ Microphone muted on pre-join screen.")

                stop_video_button = frame.get_by_role("button", name="Stop Video")
                await stop_video_button.wait_for(state="visible", timeout=10000)
                if await stop_video_button.is_enabled():
                    await stop_video_button.click()
                    print("✅ Video stopped on pre-join screen.")
            except Exception as e:
                print(f"⚠️ Could not mute or stop video, proceeding anyway: {e}")

            # Use keyboard to fill form
            await frame.locator('input[type="password"], input[type="text"]').first.focus()
            await page.keyboard.type(pwd)
            await page.keyboard.press("Tab")
            await page.keyboard.type("SHAI AI Notetaker")
            await page.keyboard.press("Tab")
            await page.keyboard.press("Tab")
            await page.keyboard.press("Enter")
            print("✅ Submitted form via keyboard.")

            # Wait for the meeting to load by looking for the "Leave" button INSIDE THE FRAME
            await frame.get_by_role("button", name=re.compile("Leave", re.I)).wait_for(state="visible", timeout=60000)
            print("✅ Successfully joined meeting.")

            job_status[job_id] = {"status": "recording"}
            recorder = subprocess.Popen(ffmpeg_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            print("✅ Audio recording started.")
            
            await asyncio.sleep(5)
            # Dismiss any "What's new" popups
            try:
                await frame.get_by_role("button", name="OK").click(timeout=5000)
                print("ℹ️ Dismissed 'OK' popup.")
            except:
                pass

            while True:
                await asyncio.sleep(5)
                if job_status.get(job_id, {}).get("status") == "stopping":
                    print("Stop signal received, leaving meeting.")
                    break
                try:
                    # Check participants INSIDE THE FRAME
                    participants_button = frame.get_by_role("button", name=re.compile("Participants", re.I))
                    participant_count_text = await participants_button.inner_text()
                    match = re.search(r'\d+', participant_count_text)
                    if match and int(match.group()) <= 1:
                        print("🚪 Only 1 participant left. Ending recording.")
                        break
                except Exception:
                    # If the button isn't found, the meeting may have ended
                    print("⚠️ Could not find participants button, assuming meeting ended.")
                    break

        except Exception as e:
            job_status[job_id] = {"status": "failed", "error": f"An error occurred in the meeting: {e}"}
            await page.screenshot(path=os.path.join(output_dir, "error.png"))
        finally:
            if recorder and recorder.poll() is None:
                recorder.terminate()
                recorder.communicate()
            try:
                # Leave the meeting from INSIDE THE FRAME
                print("Attempting to leave meeting...")
                leave_button = page.frame_locator('iframe').first.get_by_role("button", name=re.compile("Leave", re.I))
                await leave_button.click(timeout=5000)
                # Confirm leaving
                await page.get_by_role("button", name=re.compile("Leave Meeting", re.I)).click(timeout=5000)
                print("✅ Left meeting.")
            except Exception as e:
                print(f"Could not click leave button: {e}")
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
