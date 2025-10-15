import os
import re
import time
import asyncio
import subprocess
import httpx
from playwright.async_api import async_playwright, TimeoutError
from summarizer import WorkflowAgentProcessor
from caption_merger import merge_meeting_transcripts_by_time

# --- CONFIGURATION ---
WHISPERX_URL = "http://88.204.158.4:8080/v1/audio/transcriptions"
BACKEND_URL = "http://localhost:8080"


# --- FIXED: MP3 Encoding and Indefinite Recording ---
def get_ffmpeg_command(platform, output_path):
    """
    Generates the ffmpeg command for recording system audio to MP3.
    - Removed the '-t' duration limit to record until manually stopped.
    - Changed output to MP3 with a 320kbps bitrate.
    """
    if platform.startswith("linux"):
        return [
            "ffmpeg", "-y", "-f", "pulse", "-i", "default",
            "-acodec", "libmp3lame", "-b:a", "320k",
            output_path
        ]
    return None

def transcribe_audio(audio_path, transcript_path):
    if not os.path.exists(audio_path) or os.path.getsize(audio_path) < 4096:
        print(f"❌ Audio file at {audio_path} is too small. Skipping transcription.")
        return False
    
    print(f"🎤 Sending {os.path.basename(audio_path)} to whisperx for transcription...")
    try:
        with open(audio_path, 'rb') as f:
            files = {'file': (os.path.basename(audio_path), f)}
            data = {'model': 'whisper-large-v3'}
            response = httpx.post(WHISPERX_URL, files=files, data=data, timeout=600)
        
        if response.status_code == 200:
            transcript_data = response.json()
            clean_transcript = ""
            if 'segments' in transcript_data:
                 for seg in transcript_data['segments']:
                     speaker = seg.get('speaker', 'SPEAKER_UNKNOWN')
                     start = seg.get('start', 0)
                     end = seg.get('end', 0)
                     text = seg.get('text', '').strip()
                     clean_transcript += f"[{speaker}] [{start:.2f} - {end:.2f}]\n{text}\n\n"
            else:
                clean_transcript = transcript_data.get('text', '').replace('<br>', '\n')
                
            with open(transcript_path, 'w', encoding='utf-8') as f:
                f.write(clean_transcript)
            print(f"✅ Transcription successful. Saved to {transcript_path}")
            return True
        else:
            print(f"❌ Transcription failed. Status code: {response.status_code}\n{response.text}")
            return False
    except httpx.RequestError as e:
        print(f"❌ Error connecting to whisperx service: {e}")
        return False
    except Exception as e:
        print(f"An unexpected error occurred during transcription: {e}")
        return False

async def run_bot_task(meeting_url: str, job_id: str, job_status: dict):
    import sys
    
    output_dir = os.path.join("outputs", job_id)
    os.makedirs(output_dir, exist_ok=True)
    
    # --- FIXED: Use .mp3 extension ---
    output_audio_path = os.path.join(output_dir, "meeting_audio.mp3")
    output_transcript_path = os.path.join(output_dir, "transcript.txt")
    
    ffmpeg_command = get_ffmpeg_command(sys.platform, output_audio_path)
    if not ffmpeg_command:
        job_status[job_id] = {"status": "failed", "error": "Unsupported OS"}
        return

    job_status[job_id] = {"status": "starting_browser"}
    
    async with async_playwright() as p, httpx.AsyncClient() as client:
        try:
            browser = await p.chromium.launch(
                headless=False, 
                args=["--disable-blink-features=AutomationControlled", "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"]
            )
            context = await browser.new_context(permissions=["microphone", "camera"])

            async def handle_caption_event(data):
                try:
                    await client.post(f"{BACKEND_URL}/captions/{job_id}", json=data, timeout=5.0)
                except httpx.RequestError as e:
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
                mic_button = page.locator('div[data-is-muted="false"][role="button"][aria-label*="microphone"]')
                cam_button = page.locator('div[data-is-muted="false"][role="button"][aria-label*="camera"]')
                if await mic_button.is_visible(timeout=5000): await mic_button.click(); print("🎤 Microphone turned off.")
                if await cam_button.is_visible(timeout=5000): await cam_button.click(); print("📷 Camera turned off.")
            except Exception as e:
                print(f"⚠️ Could not turn off camera/mic: {e}")

            join_button_locator = page.get_by_role("button", name=re.compile("Join now|Ask to join"))
            await join_button_locator.wait_for(timeout=15000)
            
            recording_start_time_ms = int(time.time() * 1000)
            await page.evaluate(f"window.MEETING_START_TIME = {recording_start_time_ms};")
            
            job_status[job_id] = {"status": "recording"}
            print(f"▶️ Starting ffmpeg audio recorder for {output_audio_path}...")
            recorder = subprocess.Popen(ffmpeg_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            await join_button_locator.click(timeout=15000)

            await page.get_by_role("button", name="Leave call").wait_for(state="visible", timeout=45000)
            print("✅ Bot has successfully joined the meeting.")
            print("⏳ Waiting 10 seconds for meeting UI to stabilize...")
            await asyncio.sleep(10)
            
            # --- FIXED: More robust caption enabling logic ---
            captions_enabled = False
            try:
                print("💬 Attempting to enable captions...")
                caption_container_locator = page.locator('div[jsname="dsyhDe"]')
                # Try keyboard shortcut first
                await page.keyboard.press("c")
                await asyncio.sleep(1) # Give it a moment to react
                await caption_container_locator.wait_for(state="visible", timeout=3000)
                print("✅ Captions enabled via keyboard shortcut.")
                captions_enabled = True
            except TimeoutError:
                print("⚠️ Keyboard shortcut failed, trying button click...")
                try:
                    # Fallback to clicking the most likely button
                    caption_button = page.locator('button[jsname="r8qRAd"], button[aria-label*="caption"]').first
                    await caption_button.click(timeout=5000)
                    await caption_container_locator.wait_for(state="visible", timeout=3000)
                    print("✅ Captions enabled via button click.")
                    captions_enabled = True
                except Exception as btn_err:
                    print(f"❌ Button click also failed. Could not enable captions: {btn_err}")

            if captions_enabled:
                try:
                    await page.wait_for_function("() => document.querySelector('div[jsname=\"dsyhDe\"]')?.textContent?.trim().length > 0", timeout=30000)
                    print("✅ Caption UI is active. Injecting observer...")
                    
                    # --- FIXED: Restored robust MutationObserver JavaScript ---
                    await page.evaluate("""() => {
                        let lastSpeaker = 'Unknown Speaker';
                        let lastCaptionText = '';
                        let lastCaptionTime = 0;
                        const DUPLICATE_THRESHOLD_MS = 1500;
                        
                        const getSpeaker = (element) => {
                            try {
                                const speakerEl = element.querySelector('.NWpY1d');
                                if (speakerEl?.textContent?.trim()) {
                                    lastSpeaker = speakerEl.textContent.trim();
                                }
                                return lastSpeaker;
                            } catch (e) { return lastSpeaker; }
                        };
                        
                        const getCaptionText = (element) => {
                            try {
                                const textEl = element.querySelector('.ygicle');
                                return textEl ? textEl.textContent?.trim().replace(/\\s+/g, ' ').trim() : '';
                            } catch (e) { return ''; }
                        };
                        
                        const sendCaption = (speaker, text) => {
                            if (!text || !window.MEETING_START_TIME) return;
                            const now = Date.now();
                            if (text === lastCaptionText && (now - lastCaptionTime) < DUPLICATE_THRESHOLD_MS) return;
                            
                            lastCaptionText = text;
                            lastCaptionTime = now;
                            const elapsedSeconds = (now - window.MEETING_START_TIME) / 1000.0;
                            
                            if (window.onCaptionReceived) {
                                window.onCaptionReceived({ speaker, text, timestamp: elapsedSeconds });
                            }
                        };
                        
                        const processElement = (element) => {
                            if (!element || element.nodeType !== Node.ELEMENT_NODE) return;
                            const mainCaptionBlock = element.closest('.nMcdL');
                            if (mainCaptionBlock) {
                                const speaker = getSpeaker(mainCaptionBlock);
                                const text = getCaptionText(mainCaptionBlock);
                                if (text) sendCaption(speaker, text);
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
                            observer.observe(captionContainer, { childList: true, subtree: true, characterData: true });
                            console.log('[CAPTION-OBSERVER] Monitoring caption container.');
                        }
                    }""")
                    print("✅ Caption observer successfully injected.")
                except Exception as e:
                    print(f"❌ Error setting up caption observer: {e}")
        
            while True:
                await asyncio.sleep(4)
                if recorder.poll() is not None:
                    print("❌ FFMPEG recorder process stopped unexpectedly. Ending meeting.")
                    break
                if job_status.get(job_id, {}).get("status") == "stopping":
                    print("⏹️ Stop signal received, leaving meeting.")
                    break
                try:
                    locator = page.locator('button[aria-label*="Show everyone"], button[aria-label*="Participants"], button[aria-label*="People"]').first
                    await locator.wait_for(state="visible", timeout=3000)
                    count_text = await locator.get_attribute("aria-label") or ""
                    match = re.search(r'(\d+)', count_text)
                    if match:
                        participant_count = int(match.group(1))
                        print(f"👥 Participants: {participant_count}")
                        if participant_count <= 1:
                            print("👤 Only 1 participant left. Ending recording.")
                            break
                    else:
                        print("⚠️ Could not parse participant count. Leaving.")
                        break
                except (TimeoutError, AttributeError):
                    print("🚪 Could not find participant button. Leaving.")
                    break
                    
        except Exception as e:
            job_status[job_id] = {"status": "failed", "error": f"An error occurred in the meeting: {e}"}
            await page.screenshot(path=os.path.join(output_dir, "error.png"))
        finally:
            if recorder and recorder.poll() is None:
                print("🛑 Terminating ffmpeg recorder process...")
                recorder.terminate()
                try:
                    recorder.wait(timeout=10) # Give it time to finalize the MP3
                except subprocess.TimeoutExpired:
                    recorder.kill()
                print("✅ FFMPEG recorder stopped.")
            
            try:
                await page.get_by_role("button", name="Leave call").click(timeout=5000)
                await asyncio.sleep(3)
            except Exception: pass
            
            await browser.close()
    
    # Post-processing...
    if os.path.exists(output_audio_path) and os.path.getsize(output_audio_path) > 4096:
        if transcribe_audio(output_audio_path, output_transcript_path):
            merged_transcript_path = os.path.join(output_dir, "merged_transcript.txt")
            merge_success = merge_meeting_transcripts_by_time(job_id)
            final_transcript_path = merged_transcript_path if merge_success else output_transcript_path
            
            summarizer = WorkflowAgentProcessor("https://shai.pro/v1", "app-GMysC0py6j6HQJsJSxI2Rbxb")
            file_id = await summarizer.upload_file(final_transcript_path)
            
            if file_id:
                summary_pdf_path = os.path.join(output_dir, "summary.pdf")
                if await summarizer.run_workflow(file_id, summary_pdf_path):
                    job_status[job_id] = {
                        "status": "completed", 
                        "transcript_path": output_transcript_path,
                        "merged_transcript_path": final_transcript_path,
                        "summary_path": summary_pdf_path
                    }
                else: job_status[job_id] = {"status": "failed", "error": "Summarization failed."}
            else: job_status[job_id] = {"status": "failed", "error": "File upload for summarization failed."}
        else: job_status[job_id] = {"status": "failed", "error": "Transcription failed"}
    else:
        job_status[job_id] = {"status": "failed", "error": "Audio recording was empty or failed"}
