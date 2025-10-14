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
                
                # Dismiss any overlays first
                await page.keyboard.press("Escape")
                await asyncio.sleep(1)
                
                # Try keyboard shortcut (correct Playwright Python syntax)
                await page.keyboard.press("C", delay=100)
                await asyncio.sleep(1)
                await page.keyboard.press("Shift+C")
                await asyncio.sleep(2)
                
                # Check if captions are visible
                try:
                    caption_container = page.locator('div[jsname="dsyhDe"]')
                    await caption_container.wait_for(state="visible", timeout=5000)
                    print("✅ Captions enabled via keyboard shortcut.")
                    captions_enabled = True
                except TimeoutError:
                    # Fallback: try clicking the caption button
                    print("⚠️ Keyboard shortcut failed, trying button click...")
                    try:
                        # Look for captions button using multiple possible selectors
                        caption_selectors = [
                            'button[aria-label*="caption" i]',
                            'button[aria-label*="Captions" i]',
                            'div[role="button"][aria-label*="caption" i]',
                            'button[jsname="r8qRAd"]'  # Known caption button jsname
                        ]
                        
                        for selector in caption_selectors:
                            try:
                                button = page.locator(selector).first
                                if await button.is_visible(timeout=2000):
                                    await button.click()
                                    await asyncio.sleep(2)
                                    await caption_container.wait_for(state="visible", timeout=3000)
                                    print("✅ Captions enabled via button click.")
                                    captions_enabled = True
                                    break
                            except:
                                continue
                    except Exception as btn_err:
                        print(f"⚠️ Button click also failed: {btn_err}")
                    
            except Exception as e:
                screenshot_path = os.path.join(output_dir, "caption_error_screenshot.png")
                await page.screenshot(path=screenshot_path)
                print(f"📸 Screenshot saved to {screenshot_path}")
                print(f"⚠️ Could not enable captions. Error: {e}")
            
            if captions_enabled:
                try:
                    print("... Waiting for caption UI to become active...")
                    
                    # Wait for actual caption text to appear (not just the container)
                    await page.wait_for_function("""() => {
                        // Look for the caption container
                        const container = document.querySelector('div[jsname="dsyhDe"]');
                        if (!container) return false;
                        
                        // Check if it has any text content
                        const hasContent = container.textContent?.trim().length > 0;
                        return hasContent;
                    }""", timeout=30000)
                    
                    print("✅ Caption UI is active. Injecting observer...")

                    # Inject the MutationObserver based on actual DOM structure
                    await page.evaluate("""() => {
                        let lastSpeaker = 'Unknown Speaker';
                        const seenCaptions = new Map(); // Use Map with timestamp for better deduplication
                        
                        // Extract speaker name - based on actual DOM structure
                        const getSpeaker = (element) => {
                            try {
                                // Look for speaker name in various possible locations
                                const speakerSelectors = [
                                    'div[jsname="YSxPC"]',  // Common speaker name container
                                    'div[class*="zs7s8d"]', // Alternative speaker container
                                    'span[class*="speaker"]'
                                ];
                                
                                for (const selector of speakerSelectors) {
                                    const speakerEl = element.querySelector(selector);
                                    if (speakerEl && speakerEl.textContent?.trim()) {
                                        const name = speakerEl.textContent.trim();
                                        lastSpeaker = name;
                                        return name;
                                    }
                                }
                                
                                // Fallback: check parent elements
                                let current = element;
                                for (let i = 0; i < 3; i++) {
                                    if (!current) break;
                                    const possibleSpeaker = current.querySelector('[jsname="YSxPC"]');
                                    if (possibleSpeaker && possibleSpeaker.textContent?.trim()) {
                                        const name = possibleSpeaker.textContent.trim();
                                        lastSpeaker = name;
                                        return name;
                                    }
                                    current = current.parentElement;
                                }
                                
                                return lastSpeaker;
                            } catch (e) {
                                console.error('[CAPTION] Error getting speaker:', e);
                                return lastSpeaker;
                            }
                        };
                        
                        // Extract caption text - based on actual DOM structure
                        const getText = (element) => {
                            try {
                                // Try multiple possible caption text containers
                                const textSelectors = [
                                    'div[jsname="tgaKEf"]',  // Main caption text container
                                    'div[class*="iTTPOb"]',  // Alternative text container
                                    'span[class*="caption"]'
                                ];
                                
                                for (const selector of textSelectors) {
                                    const textEl = element.querySelector(selector);
                                    if (textEl && textEl.textContent?.trim()) {
                                        return textEl.textContent.trim();
                                    }
                                }
                                
                                // Fallback: get all text but remove speaker names
                                const clone = element.cloneNode(true);
                                clone.querySelectorAll('[jsname="YSxPC"], div[class*="zs7s8d"]').forEach(el => el.remove());
                                const text = clone.textContent?.trim() || '';
                                return text;
                            } catch (e) {
                                console.error('[CAPTION] Error getting text:', e);
                                return '';
                            }
                        };
                        
                        // Send caption to Python
                        const sendCaption = (element) => {
                            try {
                                const text = getText(element);
                                const speaker = getSpeaker(element);
                                
                                // Filter invalid captions
                                if (!text || text.length < 2) return;
                                
                                // Don't send if speaker name equals text
                                if (text.toLowerCase() === speaker.toLowerCase()) return;
                                
                                // Deduplication with timestamp
                                const key = `${speaker}:${text}`;
                                const now = Date.now();
                                
                                // Check if we've seen this exact caption recently (within 5 seconds)
                                if (seenCaptions.has(key)) {
                                    const lastTime = seenCaptions.get(key);
                                    if (now - lastTime < 5000) {
                                        return; // Skip duplicate
                                    }
                                }
                                
                                seenCaptions.set(key, now);
                                
                                // Clean up old entries (older than 30 seconds)
                                for (const [k, time] of seenCaptions.entries()) {
                                    if (now - time > 30000) {
                                        seenCaptions.delete(k);
                                    }
                                }
                                
                                console.log(`[CAPTION-OBSERVER] ${speaker}: ${text}`);
                                
                                if (window.onCaptionReceived) {
                                    window.onCaptionReceived({
                                        speaker: speaker,
                                        text: text,
                                        timestamp: new Date().toISOString()
                                    });
                                }
                            } catch (e) {
                                console.error('[CAPTION-OBSERVER] Error in sendCaption:', e);
                            }
                        };
                        
                        // Process any element that might contain captions
                        const processElement = (element) => {
                            if (!element || element.nodeType !== Node.ELEMENT_NODE) return;
                            
                            // Check if this is a caption container
                            const isCaptionContainer = 
                                element.matches('div[jsname="dsyhDe"]') ||
                                element.querySelector('div[jsname="dsyhDe"]') ||
                                element.matches('div[jsname="tgaKEf"]') ||
                                element.querySelector('div[jsname="tgaKEf"]');
                            
                            if (isCaptionContainer) {
                                sendCaption(element);
                            }
                            
                            // Also check children
                            if (element.children) {
                                for (const child of element.children) {
                                    processElement(child);
                                }
                            }
                        };
                        
                        // Set up MutationObserver
                        const observer = new MutationObserver((mutations) => {
                            for (const mutation of mutations) {
                                // Handle newly added elements
                                if (mutation.type === 'childList' && mutation.addedNodes.length > 0) {
                                    mutation.addedNodes.forEach(node => {
                                        processElement(node);
                                    });
                                }
                                
                                // Handle text changes in existing elements
                                if (mutation.type === 'characterData' || mutation.type === 'childList') {
                                    if (mutation.target.parentElement) {
                                        processElement(mutation.target.parentElement);
                                    }
                                }
                            }
                        });
                        
                        // Observe the caption container specifically
                        const captionContainer = document.querySelector('div[jsname="dsyhDe"]');
                        if (captionContainer) {
                            observer.observe(captionContainer, {
                                childList: true,
                                characterData: true,
                                subtree: true
                            });
                            console.log('[CAPTION-OBSERVER] Observing caption container directly');
                        }
                        
                        // Also observe body as fallback
                        observer.observe(document.body, {
                            childList: true,
                            characterData: true,
                            subtree: true
                        });
                        
                        console.log('[CAPTION-OBSERVER] Observer is now active and monitoring for captions.');
                    }""")
                    
                    print("✅ Caption observer successfully injected and running.")
                    
                except TimeoutError:
                    print("❌ Timed out waiting for first caption to appear. Recording audio only.")
                except Exception as e:
                    print(f"❌ Error setting up caption observer: {e}")
                    import traceback
                    traceback.print_exc()

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
