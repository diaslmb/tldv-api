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
            browser = await p.chromium.launch(
                headless=False, 
                args=[
                    "--disable-blink-features=AutomationControlled", 
                    "--use-fake-ui-for-media-stream", 
                    "--use-fake-device-for-media-stream"
                ]
            )
            context = await browser.new_context(permissions=["microphone", "camera"])

            async def handle_caption_event(data):
                speaker = data.get('speaker', 'Unknown')
                text = data.get('text', '')
                print(f"✅ CAPTION from [{speaker}]: {text}")
                try:
                    requests.post(f"{BACKEND_URL}/captions/{job_id}", json=data, timeout=5)
                except requests.exceptions.RequestException as e:
                    print(f"❌ Could not send caption to backend: {e}")

            await context.expose_binding(
                "onCaptionReceived", 
                lambda source, data: asyncio.create_task(handle_caption_event(data))
            )
            
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
            
            # Turn off camera and microphone
            try:
                mic_button = page.locator('div[data-is-muted="false"][role="button"][aria-label*="microphone"]')
                cam_button = page.locator('div[data-is-muted="false"][role="button"][aria-label*="camera"]')
                
                if await mic_button.is_visible(timeout=5000):
                    await mic_button.click()
                    print("🎤 Microphone turned off.")
                
                if await cam_button.is_visible(timeout=5000):
                    await cam_button.click()
                    print("📷 Camera turned off.")
            except Exception as e:
                print(f"⚠️ Could not turn off camera/mic (they may already be off): {e}")

            join_button_locator = page.get_by_role("button", name=re.compile("Join now|Ask to join"))
            await join_button_locator.wait_for(timeout=15000)
            
            job_status[job_id] = {"status": "recording"}
            print("▶️ Starting ffmpeg audio recorder...")
            recorder = subprocess.Popen(ffmpeg_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            await join_button_locator.click(timeout=15000)

            await page.get_by_role("button", name="Leave call").wait_for(state="visible", timeout=45000)
            print("✅ Bot has successfully joined the meeting.")

            # Enable captions
            captions_enabled = False
            try:
                print("💬 Attempting to enable captions...")
                # Try pressing Shift+C first (keyboard shortcut)
                await page.keyboard.press("c", modifiers=["Shift"])
                await asyncio.sleep(2)
                
                # Verify captions are enabled by looking for aria-live region
                try:
                    await page.wait_for_selector('[aria-live]', timeout=5000)
                    print("✅ Captions enabled via keyboard shortcut.")
                    captions_enabled = True
                except TimeoutError:
                    # Fallback: try clicking the caption button
                    caption_button = page.get_by_role("button", name=re.compile("caption", re.IGNORECASE))
                    await caption_button.click(timeout=7000)
                    await page.wait_for_selector('[aria-live]', timeout=5000)
                    print("✅ Captions enabled via button click.")
                    captions_enabled = True
                    
            except Exception as e:
                screenshot_path = os.path.join(output_dir, "caption_error_screenshot.png")
                await page.screenshot(path=screenshot_path)
                print(f"📸 Screenshot saved to {screenshot_path}")
                print(f"⚠️ Could not enable captions. Error: {e}")
            
            if captions_enabled:
                try:
                    print("... Waiting for caption UI to become active...")
                    
                    # Wait for the aria-live region to have content
                    await page.wait_for_function("""() => {
                        const liveRegions = Array.from(document.querySelectorAll('[aria-live]'));
                        return liveRegions.some(el => el.textContent?.trim().length > 0);
                    }""", timeout=20000)
                    
                    print("✅ Caption UI is active. Injecting observer...")

                    # Inject the MutationObserver to scrape captions
                    await page.evaluate("""() => {
                        const SPEAKER_BADGE_SELECTOR = '.NWpY1d, .xoMHSc, [class*="speaker"]';
                        let lastSpeaker = 'Unknown Speaker';
                        const seenCaptions = new Set();
                        
                        // Extract speaker name from caption element
                        const getSpeaker = (node) => {
                            try {
                                const badge = node.querySelector(SPEAKER_BADGE_SELECTOR);
                                if (badge && badge.textContent?.trim()) {
                                    const speaker = badge.textContent.trim();
                                    lastSpeaker = speaker;
                                    return speaker;
                                }
                                return lastSpeaker;
                            } catch (e) {
                                return lastSpeaker;
                            }
                        };
                        
                        // Extract caption text (remove speaker badge from text)
                        const getText = (node) => {
                            try {
                                const clone = node.cloneNode(true);
                                // Remove speaker badges from clone
                                clone.querySelectorAll(SPEAKER_BADGE_SELECTOR).forEach(el => el.remove());
                                const text = clone.textContent?.trim() || '';
                                return text;
                            } catch (e) {
                                return '';
                            }
                        };
                        
                        // Send caption to Python
                        const sendCaption = (node) => {
                            try {
                                const text = getText(node);
                                const speaker = getSpeaker(node);
                                
                                // Filter out empty captions and duplicates
                                if (!text || text.length < 2) return;
                                
                                // Don't send if speaker name is in the text (likely malformed)
                                const textLower = text.toLowerCase();
                                const speakerLower = speaker.toLowerCase();
                                if (textLower === speakerLower) return;
                                
                                // Create unique key for deduplication
                                const key = `${speaker}:${text}`;
                                if (seenCaptions.has(key)) return;
                                seenCaptions.add(key);
                                
                                // Keep set size manageable
                                if (seenCaptions.size > 100) {
                                    const firstKey = seenCaptions.values().next().value;
                                    seenCaptions.delete(firstKey);
                                }
                                
                                console.log(`[CAPTION-OBSERVER] ${speaker}: ${text}`);
                                window.onCaptionReceived({
                                    speaker: speaker,
                                    text: text,
                                    timestamp: new Date().toISOString()
                                });
                            } catch (e) {
                                console.error('[CAPTION-OBSERVER] Error in sendCaption:', e);
                            }
                        };
                        
                        // Set up MutationObserver
                        const observer = new MutationObserver((mutations) => {
                            for (const mutation of mutations) {
                                // Handle newly added caption elements
                                if (mutation.type === 'childList' && mutation.addedNodes.length > 0) {
                                    mutation.addedNodes.forEach(node => {
                                        if (node.nodeType === Node.ELEMENT_NODE) {
                                            // Check if this node or its children contain caption text
                                            if (node.textContent?.trim().length > 0) {
                                                sendCaption(node);
                                            }
                                        }
                                    });
                                }
                                
                                // Handle text changes in existing elements (live updates)
                                if (mutation.type === 'characterData' && mutation.target?.parentElement) {
                                    sendCaption(mutation.target.parentElement);
                                }
                            }
                        });
                        
                        // Observe the entire document body for caption changes
                        observer.observe(document.body, {
                            childList: true,
                            characterData: true,
                            subtree: true
                        });
                        
                        console.log('[CAPTION-OBSERVER] Observer is now active and monitoring for captions.');
                    }""")
                    
                    print("✅ Caption observer successfully injected and running.")
                    
                except TimeoutError:
                    print("❌ Timed out waiting for first caption to appear. Scraping may not work properly.")
                except Exception as e:
                    print(f"❌ Error setting up caption observer: {e}")

            # Wait a bit for initial captions to be captured
            await asyncio.sleep(10)

            # Main monitoring loop
            while True:
                await asyncio.sleep(4)
                
                # Check if ffmpeg is still running
                if recorder.poll() is not None:
                    print("❌ FFMPEG recorder process has stopped unexpectedly. Ending meeting.")
                    break
                
                # Check for stop signal
                try:
                    if job_status.get(job_id, {}).get("status") == "stopping":
                        print("⏹️ Stop signal received, leaving meeting.")
                        break
                    
                    # Check participant count
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
            # Stop ffmpeg recorder
            if recorder and recorder.poll() is None:
                print("🛑 Terminating ffmpeg recorder process...")
                recorder.terminate()
                try: 
                    recorder.wait(timeout=5)
                except subprocess.TimeoutExpired: 
                    recorder.kill()
                print("✅ FFMPEG recorder stopped.")
            
            # Leave the meeting
            try:
                await page.get_by_role("button", name="Leave call").click(timeout=5000)
                await asyncio.sleep(3)
            except Exception: 
                pass
            
            await browser.close()

    # Post-processing: transcription and summarization
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
