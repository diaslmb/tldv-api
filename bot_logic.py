import os
import re
import time # IMPORTED
import asyncio
import subprocess
from playwright.async_api import async_playwright, TimeoutError
import requests
import uuid
from summarizer import WorkflowAgentProcessor
from caption_merger import merge_meeting_transcripts_by_time # UPDATED IMPORT

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
            # WhisperX can return text or segments, we handle both for robustness
            if 'text' in transcript_data:
                clean_transcript = transcript_data.get('text', '').replace('<br>', '\n')
            elif 'segments' in transcript_data:
                 # Reconstruct the text format you were using
                 clean_transcript = ""
                 for seg in transcript_data['segments']:
                     speaker = seg.get('speaker', 'SPEAKER_UNKNOWN')
                     start = seg.get('start', 0)
                     end = seg.get('end', 0)
                     text = seg.get('text', '').strip()
                     clean_transcript += f"[{speaker}] [{start:.2f} - {end:.2f}]\n {text}\n\n"
            else:
                clean_transcript = ""
                
            with open(transcript_path, 'w', encoding='utf-8') as f:
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
            
            # --- CAPTURE T0 AND START RECORDING ---
            recording_start_time_ms = int(time.time() * 1000)
            await page.evaluate(f"window.MEETING_START_TIME = {recording_start_time_ms};")
            
            job_status[job_id] = {"status": "recording"}
            print(f"▶️ Starting ffmpeg audio recorder... (T0={recording_start_time_ms})")
            recorder = subprocess.Popen(ffmpeg_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            await join_button_locator.click(timeout=15000)

            await page.get_by_role("button", name="Leave call").wait_for(state="visible", timeout=45000)
            print("✅ Bot has successfully joined the meeting.")

            # Enable captions logic remains the same...
            captions_enabled = False
            try:
                print("💬 Attempting to enable captions...")
                await page.keyboard.press("Escape")
                await asyncio.sleep(1)
                await page.keyboard.press("C", delay=100)
                await asyncio.sleep(1)
                await page.keyboard.press("Shift+C")
                await asyncio.sleep(2)
                caption_container_locator = page.locator('div[jsname="dsyhDe"]')
                await caption_container_locator.wait_for(state="visible", timeout=5000)
                print("✅ Captions enabled.")
                captions_enabled = True
            except TimeoutError:
                print("⚠️ Caption keyboard shortcut/check failed. Trying button.")
                # Fallback to button click...
                try:
                    caption_button = page.locator('button[jsname="r8qRAd"]').first
                    await caption_button.click(timeout=5000)
                    await caption_container_locator.wait_for(state="visible", timeout=5000)
                    print("✅ Captions enabled via button click.")
                    captions_enabled = True
                except Exception as e:
                    print(f"⚠️ Could not enable captions via button either: {e}")
            
            if captions_enabled:
                try:
                    print("... Waiting for caption UI to become active...")
                    await page.wait_for_function("() => document.querySelector('div[jsname=\"dsyhDe\"]')?.textContent?.trim().length > 0", timeout=30000)
                    print("✅ Caption UI is active. Injecting observer...")

                    # --- UPDATED JAVASCRIPT INJECTION ---
                    await page.evaluate("""() => {
                        let lastSpeaker = 'Unknown Speaker';
                        let lastCaptionText = '';
                        let lastCaptionTime = 0;
                        const DUPLICATE_THRESHOLD_MS = 2000;
                        
                        const getSpeaker = (element) => {
                            try {
                                const speakerEl = element.querySelector('.NWpY1d');
                                if (speakerEl?.textContent?.trim()) {
                                    lastSpeaker = speakerEl.textContent.trim();
                                    return lastSpeaker;
                                }
                                return lastSpeaker;
                            } catch (e) { console.error('[CAPTION] Error getting speaker:', e); return lastSpeaker; }
                        };
                        
                        const getCaptionText = (element) => {
                            try {
                                const textEl = element.querySelector('.ygicle');
                                return textEl ? textEl.textContent?.trim().replace(/\\s+/g, ' ').trim() : '';
                            } catch (e) { console.error('[CAPTION] Error getting text:', e); return ''; }
                        };
                        
                        const sendCaption = (speaker, text) => {
                            try {
                                if (!text || !window.MEETING_START_TIME) return;
                                
                                const now = Date.now();
                                if (text === lastCaptionText && (now - lastCaptionTime) < DUPLICATE_THRESHOLD_MS) return;
                                
                                lastCaptionText = text;
                                lastCaptionTime = now;
                                
                                // CALCULATE RELATIVE TIMESTAMP
                                const elapsedSeconds = (now - window.MEETING_START_TIME) / 1000.0;
                                
                                console.log(`[CAPTION-CLEAN | ${elapsedSeconds.toFixed(2)}s] ${speaker}: ${text}`);
                                
                                if (window.onCaptionReceived) {
                                    window.onCaptionReceived({
                                        speaker: speaker,
                                        text: text,
                                        timestamp: elapsedSeconds // SEND RELATIVE TIME
                                    });
                                }
                            } catch (e) { console.error('[CAPTION] Error sending caption:', e); }
                        };
                        
                        const processElement = (element) => {
                            if (!element || element.nodeType !== Node.ELEMENT_NODE) return;
                            const mainCaptionBlock = element.closest('.nMcdL');
                            if (mainCaptionBlock) {
                                const speaker = getSpeaker(mainCaptionBlock);
                                const text = getCaptionText(mainCaptionBlock);
                                if (text && text.length >= 2) { sendCaption(speaker, text); }
                            }
                        };
                        
                        const observer = new MutationObserver((mutations) => {
                            for (const mutation of mutations) {
                                if (mutation.type === 'childList') {
                                    mutation.addedNodes.forEach(node => processElement(node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement));
                                } else if (mutation.type === 'characterData') {
                                    processElement(mutation.target.parentElement);
                                }
                            }
                        });
                        
                        const captionContainer = document.querySelector('div[jsname="dsyhDe"]');
                        if (captionContainer) {
                            observer.observe(captionContainer, { childList: true, characterData: true, subtree: true });
                            console.log('[CAPTION-OBSERVER] Monitoring caption container');
                        } else {
                            console.warn('[CAPTION-OBSERVER] Caption container not found!');
                        }
                    }""")
                    
                    print("✅ Caption observer successfully injected and running.")
                    
                except TimeoutError:
                    print("❌ Timed out waiting for first caption. Recording audio only.")
                except Exception as e:
                    print(f"❌ Error setting up caption observer: {e}")
            
            # Main monitoring loop remains the same...
            while True:
                await asyncio.sleep(4)
                if recorder.poll() is not None:
                    print("❌ FFMPEG recorder process stopped unexpectedly. Ending meeting.")
                    break
                try:
                    if job_status.get(job_id, {}).get("status") == "stopping":
                        print("⏹️ Stop signal received, leaving meeting.")
                        break
                    locator = page.locator('button[aria-label*="Show everyone"], button[aria-label*="Participants"]').first
                    await locator.wait_for(state="visible", timeout=3000)
                    count_text = await locator.get_attribute("aria-label") or ""
                    match = re.search(r'(\d+)', count_text)
                    if match and int(match.group(1)) <= 1:
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

    # Post-processing
    if os.path.exists(output_audio_path) and os.path.getsize(output_audio_path) > 4096:
        job_status[job_id] = {"status": "transcribing"}
        if transcribe_audio(output_audio_path, output_transcript_path):
            
            job_status[job_id] = {"status": "merging_transcripts"}
            print("🔄 Merging captions with STT transcript by time...")
            merged_transcript_path = os.path.join(output_dir, "merged_transcript.txt")
            
            # --- USE NEW TIME-BASED MERGER ---
            merge_success = merge_meeting_transcripts_by_time(job_id)
            
            if merge_success:
                print("✅ Transcripts merged successfully")
                final_transcript_path = merged_transcript_path
            else:
                print("⚠️ Merge failed, using STT transcript only")
                final_transcript_path = output_transcript_path
            
            job_status[job_id] = {"status": "summarizing"}
            summarizer = WorkflowAgentProcessor("https://shai.pro/v1", "app-GMysC0py6j6HQJsJSxI2Rbxb")
            file_id = await summarizer.upload_file(final_transcript_path)
            
            if file_id:
                summary_pdf_path = os.path.join(output_dir, "summary.pdf")
                if await summarizer.run_workflow(file_id, summary_pdf_path):
                    job_status[job_id] = {
                        "status": "completed", 
                        "transcript_path": output_transcript_path,
                        "merged_transcript_path": merged_transcript_path if merge_success else None,
                        "summary_path": summary_pdf_path
                    }
                else:
                    job_status[job_id] = {"status": "failed", "error": "Summarization failed."}
            else:
                job_status[job_id] = {"status": "failed", "error": "File upload for summarization failed."}
        else:
            job_status[job_id] = {"status": "failed", "error": "Transcription failed"}
    else:
        job_status[job_id] = {"status": "failed", "error": "Audio recording was empty or failed"}
