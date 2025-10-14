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
WHISPERX_URL = "http://88.204.158.4:8080/v1/audio/transcriptions"
BACKEND_URL = "http://localhost:8080"


def get_ffmpeg_command(platform, duration, output_path):
    if platform.startswith("linux"):
        return ["ffmpeg", "-y", "-f", "pulse", "-i", "default", "-t", str(duration), output_path]
    return None

def transcribe_audio(audio_path, transcript_path):
    if not os.path.exists(audio_path):
        print(f"❌ Audio file not found at {audio_path}")
        return False
    if os.path.getsize(audio_path) < 4096:
        print(f"❌ Audio file at {audio_path} is too small to be valid. Skipping transcription.")
        return False

    print(f"🎤 Sending {audio_path} to whisperx for transcription...")
    try:
        with open(audio_path, 'rb') as f:
            files = {'file': (os.path.basename(audio_path), f)}
            data = {'model': 'whisper-large-v3'}
            response = requests.post(WHISPERX_URL, files=files, data=data, timeout=600)
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

            async def handle_caption_event(data):
                print(f"PY CAPTION from [{data.get('name')}]: {data.get('text')}")
                try:
                    requests.post(f"{BACKEND_URL}/captions/{job_id}", json=data)
                except requests.exceptions.RequestException as e:
                    print(f"❌ Could not send caption to backend: {e}")

            await context.expose_binding("onCaptionReceived", lambda source, data: asyncio.create_task(handle_caption_event(data)))
            
            page = await context.new_page()

        except Exception as e:
            job_status[job_id] = {"status": "failed", "error": f"Failed to launch browser: {e}"}
            return
            
        recorder = None
        try:
            job_status[job_id] = {"status": "navigating"}
            await page.goto(meeting_url, timeout=60000)
            await page.locator('input[placeholder="Your name"]').fill("SHAI.PRO Notetaker")
            
            try:
                await page.get_by_role("button", name="Turn off microphone").click(timeout=10000)
                await page.get_by_role("button", name="Turn off camera").click(timeout=10000)
            except Exception: pass

            join_button_locator = page.get_by_role("button", name=re.compile("Join now|Ask to join"))
            await join_button_locator.wait_for(timeout=15000)
            
            job_status[job_id] = {"status": "recording"}
            print("▶️ Starting ffmpeg audio recorder...")
            recorder = subprocess.Popen(ffmpeg_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            await join_button_locator.click(timeout=15000)

            await page.get_by_role("button", name="Leave call").wait_for(state="visible", timeout=45000)
            print("✅ Bot has successfully joined the meeting.")

            # --- START: MODIFIED CAPTION BLOCK WITH DEBUG SCREENSHOT ---
            try:
                print("💬 Attempting to enable captions...")
                await page.get_by_role("button", name="More options").click(timeout=10000)
                await page.get_by_role("menuitem", name=re.compile("Turn on captions", re.IGNORECASE)).click(timeout=10000)
                print("✅ Clicked 'Turn on captions'.")
                await asyncio.sleep(3)
            except Exception as e:
                # THIS IS THE NEW DEBUGGING CODE
                screenshot_path = os.path.join(output_dir, "caption_error_screenshot.png")
                await page.screenshot(path=screenshot_path)
                print(f"📸 Screenshot saved to {screenshot_path}")
                print(f"⚠️ Could not enable captions automatically. Will proceed without them. Error: {e}")
            # --- END: MODIFIED CAPTION BLOCK ---

            await page.evaluate("""() => {
                const CAPTION_CONTAINER_SELECTOR = '[jscontroller="YwBA9"]'; 
                const targetNode = document.querySelector(CAPTION_CONTAINER_SELECTOR);

                if (!targetNode) {
                    console.error('Could not find caption container element. Scraping will not work.');
                    return;
                }

                const observer = new MutationObserver((mutationsList) => {
                    for(const mutation of mutationsList) {
                        if (mutation.type === 'childList' && mutation.addedNodes.length > 0) {
                            mutation.addedNodes.forEach(node => {
                                if (node.nodeType !== Node.ELEMENT_NODE) return;

                                const speakerElement = node.querySelector('[data-id]');
                                const textElement = node.querySelector('span');
                                
                                if(speakerElement && textElement) {
                                    const speakerName = speakerElement.dataset.id;
                                    const captionText = textElement.innerText;
                                    
                                    if(speakerName && captionText) {
                                        window.onCaptionReceived({
                                            name: speakerName,
                                            text: captionText,
                                            timestamp: new Date().toISOString()
                                        });
                                    }
                                }
                            });
                        }
                    }
                });
                observer.observe(targetNode, { childList: true, subtree: true });
                console.log("✅ Caption observer is running inside the browser.");
            }""")

            await asyncio.sleep(10)

            while True:
                await asyncio.sleep(4)
                
                if recorder.poll() is not None:
                    print("❌ FFMPEG recorder process has stopped unexpectedly. Ending meeting.")
                    break

                try:
                    if job_status.get(job_id, {}).get("status") == "stopping":
                        print("⏹️ Stop signal received, leaving meeting.")
                        break

                    locator = page.locator('button[aria-label*="Show everyone"], button[aria-label*="Participants"], button[aria-label*="People"]').first
                    await locator.wait_for(state="visible", timeout=3000)
                    count_text = await locator.get_attribute("aria-label") or ""
                    match = re.search(r'\d+', count_text)
                    if match and int(match.group()) <= 1:
                        print("👤 Only 1 participant left. Ending recording.")
                        break
                except (TimeoutError, AttributeError):
                    print("🚪 Could not find participant count or meeting ended. Leaving.")
                    break
        except Exception as e:
            job_status[job_id] = {"status": "failed", "error": f"An error occurred in the meeting: {e}"}
            await page.screenshot(path=os.path.join(output_dir, "error.png"))
        finally:
            if recorder and recorder.poll() is None:
                print("🛑 Terminating ffmpeg recorder process...")
                recorder.terminate()
                try:
                    recorder.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    recorder.kill()
                print("✅ FFMPEG recorder stopped.")

            try:
                await page.get_by_role("button", name="Leave call").click(timeout=5000)
                await asyncio.sleep(3)
            except Exception: pass
            
            await browser.close()

    if os.path.exists(output_audio_path) and os.path.getsize(output_audio_path) > 4096:
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
