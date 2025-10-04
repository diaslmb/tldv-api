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

            await asyncio.sleep(2)
            await page.screenshot(path=os.path.join(output_dir, "1_pre_join_screen.png"))
            print("📸 Screenshot saved: 1_pre_join_screen.png")

            # --- MICROPHONE TURN OFF - TARGETED APPROACH ---
            try:
                print("🎤 Attempting to turn off microphone...")
                await asyncio.sleep(1.5)
                
                # In the Teams pre-join screen, there are toggle switches
                # The microphone toggle is the left one (first one)
                # Look for the actual clickable toggle element
                
                # Strategy 1: Find the toggle by its position (first toggle in the UI)
                try:
                    # Click at the coordinates where the microphone toggle usually is
                    # This is more reliable than trying to find the exact element
                    toggle_area = page.locator('[data-tid="toggle-button"], .ui-toggle, .ms-Toggle').first
                    await toggle_area.click(timeout=5000)
                    print("✅ Clicked microphone toggle via toggle element")
                    await asyncio.sleep(1)
                except Exception as e:
                    print(f"Toggle element click failed: {e}")
                    
                    # Strategy 2: Click the icon itself (microphone icon)
                    try:
                        # The SVG microphone icon
                        mic_icon_parent = page.locator('[role="img"]').first
                        await mic_icon_parent.click(timeout=5000)
                        print("✅ Clicked microphone icon")
                        await asyncio.sleep(1)
                    except Exception as e2:
                        print(f"Icon click failed: {e2}")
                        
                        # Strategy 3: Keyboard shortcut as last resort
                        await page.keyboard.press('Control+Shift+M')
                        print("✅ Used Ctrl+Shift+M shortcut")
                        await asyncio.sleep(1)
                
                await page.screenshot(path=os.path.join(output_dir, "1b_after_mic_toggle.png"))
                
            except Exception as e:
                print(f"⚠️ Microphone toggle error: {e}")

            join_button_locator = page.get_by_role("button", name="Join now")
            await join_button_locator.wait_for(timeout=15000)

            job_status[job_id] = {"status": "recording"}
            recorder = subprocess.Popen(ffmpeg_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            await join_button_locator.click(timeout=15000)

            await page.get_by_role("button", name=re.compile("Leave|Hang up|Выйти", re.IGNORECASE)).wait_for(state="visible", timeout=45000)
            print("✅ Bot has successfully joined the meeting.")
            await asyncio.sleep(10)  # Give more time for meeting UI to fully load

            while True:
                await asyncio.sleep(5)

                try:
                    if job_status.get(job_id, {}).get("status") == "stopping":
                        print("Stop signal received, leaving meeting.")
                        break

                    # Check if in lobby
                    lobby_text_pattern = re.compile("waiting for others to join|Someone in the meeting should let you in soon|Ожидание присоединения других участников", re.IGNORECASE)
                    is_in_lobby = await page.get_by_text(lobby_text_pattern).is_visible()
                    
                    if is_in_lobby:
                        print("🕒 Bot is in the lobby, waiting...")
                        continue

                    # --- PARTICIPANT COUNT WITH LOBBY CHECK ---
                    participant_count = 2  # Safe default
                    
                    # First, check if we were kicked to lobby (meeting ended by host)
                    lobby_kicked_patterns = [
                        "The meeting has ended",
                        "You're the only one here",
                        "Everyone else left",
                        "Встреча завершена",
                        "Вы остались один"
                    ]
                    
                    page_text_for_lobby = await page.inner_text('body')
                    for pattern in lobby_kicked_patterns:
                        if pattern.lower() in page_text_for_lobby.lower():
                            print(f"🚪 Meeting ended: '{pattern}' detected. Leaving.")
                            break
                    else:
                        # Not kicked to lobby, proceed with normal participant count
                        try:
                            participant_button = page.get_by_role("button", name=re.compile("People|Participants|Участники", re.IGNORECASE)).first
                            
                            if await participant_button.is_visible(timeout=3000):
                                await participant_button.click()
                                await asyncio.sleep(2.5)
                                
                                await page.screenshot(path=os.path.join(output_dir, f"participants_panel_{asyncio.get_event_loop().time()}.png"))
                                
                                try:
                                    page_text = await page.inner_text('body')
                                    
                                    patterns = [
                                        r'In this meeting[^\d]*\((\d+)\)',
                                        r'В этой встрече[^\d]*\((\d+)\)',
                                        r'Participants[^\d]*\((\d+)\)',
                                        r'Участники[^\d]*\((\d+)\)'
                                    ]
                                    
                                    for pattern in patterns:
                                        match = re.search(pattern, page_text, re.IGNORECASE)
                                        if match:
                                            participant_count = int(match.group(1))
                                            print(f"👥 Found participant count in text: {participant_count}")
                                            break
                                    
                                    if participant_count == 2:
                                        panel = page.locator('[role="complementary"]').first
                                        if await panel.is_visible():
                                            participant_elements = await panel.locator('[role="listitem"]').all()
                                            alt_count = len(participant_elements)
                                            if alt_count > 0:
                                                participant_count = alt_count
                                                print(f"👥 Counted {participant_count} participant listitems")
                                
                                except Exception as e:
                                    print(f"⚠️ Could not parse participant count from panel: {e}")
                                
                                await participant_button.click()
                                await asyncio.sleep(1)
                            else:
                                print("⚠️ Participants button not visible")
                                
                        except Exception as e:
                            print(f"⚠️ Error accessing participants: {e}")

                        # EXIT LOGIC: Leave only when 1 or fewer participants
                        if participant_count <= 1:
                            print(f"🚪 Only {participant_count} participant(s) remaining. Leaving meeting.")
                            break
                        else:
                            print(f"✅ {participant_count} participants still present. Monitoring...")

                except Exception as e:
                    print(f"❌ Error in monitoring loop: {e}")
                    await page.screenshot(path=os.path.join(output_dir, "monitoring_error.png"))

        except Exception as e:
            job_status[job_id] = {"status": "failed", "error": f"An error occurred in the meeting: {e}"}
            await page.screenshot(path=os.path.join(output_dir, "error.png"))
        finally:
            if recorder and recorder.poll() is None:
                recorder.terminate()
                recorder.communicate()

            try:
                await page.get_by_role("button", name=re.compile("Leave|Hang up|Выйти", re.IGNORECASE)).click(timeout=5000)
                await asyncio.sleep(3)
            except Exception:
                pass

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
