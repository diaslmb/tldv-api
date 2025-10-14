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
    if not os.path.exists(audio_path) or os.path.getsize(audio_path) < 4096:
        print(f"❌ Audio file at {audio_path} is too small. Skipping transcription.")
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
                print(f"✅ PY CAPTION from [{data.get('name')}]: {data.get('text')}")
                try:
                    requests.post(f"{BACKEND_URL}/captions/{job_id}", json=data)
                except requests.exceptions.RequestException as e:
                    print(f"❌ Could not send caption to backend: {e}")

            await context.expose_binding("onCaptionReceived", lambda source, data: asyncio.create_task(handle_caption_event(data)))
            
            page = await context.new_page()
            page.on("console", lambda msg: print(f"BROWSER LOG ({msg.type}): {msg.text}"))

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

            captions_enabled = False
            try:
                print("💬 Attempting to enable captions (Method 1: Direct Button)...")
                caption_button = page.get_by_role("button", name=re.compile("caption", re.IGNORECASE))
                await caption_button.click(timeout=7000)
                print("✅ Clicked direct caption button.")
                captions_enabled = True
            except Exception:
                print("⚠️ Method 1 failed. Trying Method 2 (More Options Menu)...")
                try:
                    await page.get_by_role("button", name="More options").click(timeout=5000)
                    await page.get_by_role("menuitem", name=re.compile("Turn on captions", re.IGNORECASE)).click(timeout=5000)
                    print("✅ Clicked 'Turn on captions' via menu.")
                    captions_enabled = True
                except Exception as e:
                    screenshot_path = os.path.join(output_dir, "caption_error_screenshot.png")
                    await page.screenshot(path=screenshot_path)
                    print(f"📸 Screenshot saved to {screenshot_path}")
                    print(f"⚠️ Both methods failed. Could not enable captions. Error: {e}")
            
            if captions_enabled:
                await asyncio.sleep(3)
                # --- START: FINAL, MOST ROBUST JAVASCRIPT SCRAPER ---
                await page.evaluate("""() => {
                    // This is the most reliable selector for the parent of all caption lines.
                    const CAPTION_CONTAINER_SELECTOR = 'div.VfPpkd-T0kwCb';
                    
                    const targetNode = document.querySelector(CAPTION_CONTAINER_SELECTOR);

                    if (!targetNode) {
                        console.error('---BROWSER--- CRITICAL: Could not find the main caption container with selector ' + CAPTION_CONTAINER_SELECTOR + '. Scraping will fail.');
                        return;
                    }
                    console.log("---BROWSER--- Successfully found caption container. Attaching observer.");

                    const observer = new MutationObserver((mutations) => {
                        for (const mutation of mutations) {
                            if (mutation.type === 'childList' && mutation.addedNodes.length > 0) {
                                mutation.addedNodes.forEach(node => {
                                    if (node.nodeType !== Node.ELEMENT_NODE) return;
                                    
                                    // The speaker's name is in the 'data-id' attribute.
                                    const speakerName = node.dataset.id || 'Unknown Speaker';
                                    
                                    // The caption text is inside a child div with this specific, stable jsname.
                                    const textElement = node.querySelector('div[jsname="jleFbf"]');
                                    const captionText = textElement ? textElement.textContent.trim() : '';

                                    if (captionText) {
                                        console.log(`---BROWSER--- SUCCESS: Found caption for '${speakerName}': '${captionText}'`);
                                        window.onCaptionReceived({
                                            name: speakerName,
                                            text: captionText,
                                            timestamp: new Date().toISOString()
                                        });
                                    }
                                });
                            }
                        }
                    });

                    observer.observe(targetNode, { childList: true });
                    console.log("---BROWSER--- FINAL VERSION 2.0 of caption observer is running.");
                }""")
                # --- END: FINAL, MOST ROBUST JAVASCRIPT SCRAPER ---

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
                    match = re.search(r'\\d+', count_text)
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
                try: recorder.wait(timeout=5)
                except subprocess.TimeoutExpired: recorder.kill()
                print("✅ FFMPEG recorder stopped.")
            try:
                await page.get_by_role("button", name="Leave call").click(timeout=5000)
                await asyncio.sleep(3)
            except Exception: pass
            
            await browser.close()

    if os.path.exists(output_audio_path) and os.path.getsize(output_audio_path) > 4096:
        job_status[job_id] = {"status": "transcribing"}
        if transcribe_audio(output_audio_path, output_transcript_path):
            job_status[job_id] = {"status": "summarizing"}
            summarizer = WorkflowAgentProcessor(base_url="https://shai.pro/v1", api_key="app-GMysC0py6j6HQJsJSxI2Rbxb")
            file_id = await summarizer.upload_file(output_transcript_path)
            if file_id:
                summary_pdf_path = os.path.join(output_dir, "summary.pdf")
                if await summarizer.run_workflow(file_id, summary_pdf_path):
                    job_status[job_id] = {"status": "completed", "transcript_path": output_transcript_path, "summary_path": summary_pdf_path}
                else:
                    job_status[job_id] = {"status": "failed", "error": "Summarization failed."}
            else:
                 job_status[job_id] = {"status": "failed", "error": "File upload for summarization failed."}
        else:
            job_status[job_id] = {"status": "failed", "error": "Transcription failed"}
    else:
        job_status[job_id] = {"status": "failed", "error": "Audio recording was empty or failed"}
