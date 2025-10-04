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

            await page.screenshot(path=os.path.join(output_dir, "1_pre_join_screen.png"))
            print("📸 Screenshot saved: 1_pre_join_screen.png")

            # --- IMPROVED MICROPHONE TURN OFF LOGIC ---
            try:
                # Try multiple strategies to turn off the microphone
                
                # Strategy 1: Look for microphone toggle button by aria-label
                try:
                    mic_button = page.locator('button[aria-label*="microphone" i], button[aria-label*="Mute" i], button[aria-label*="Микрофон" i]').first
                    await mic_button.wait_for(state="visible", timeout=5000)
                    
                    # Check if it's currently unmuted (aria-pressed="false" or aria-checked="false")
                    aria_pressed = await mic_button.get_attribute("aria-pressed")
                    aria_checked = await mic_button.get_attribute("aria-checked")
                    
                    if aria_pressed == "false" or aria_checked == "false":
                        print("🎤 Microphone is ON, clicking to turn it OFF.")
                        await mic_button.click()
                        await asyncio.sleep(1)
                        print("✅ Microphone turned OFF via button.")
                    else:
                        print("🎤 Microphone is already OFF (via button check).")
                    continue_to_join = True
                except Exception as e:
                    print(f"Strategy 1 failed: {e}")
                    continue_to_join = False
                
                # Strategy 2: Look for toggle switches
                if not continue_to_join:
                    try:
                        mic_switch = page.locator('div[role="switch"]').first
                        await mic_switch.wait_for(state="visible", timeout=5000)
                        
                        is_mic_on = await mic_switch.get_attribute("aria-checked")
                        if is_mic_on == "true":
                            print("🎤 Microphone is ON (switch), attempting to turn it OFF.")
                            await mic_switch.click()
                            await asyncio.sleep(1)
                            print("✅ Microphone turned OFF via switch.")
                        else:
                            print("🎤 Microphone is already OFF (via switch check).")
                    except Exception as e:
                        print(f"Strategy 2 failed: {e}")
                        
            except Exception as e:
                print(f"⚠️ Could not change microphone state: {e}")

            join_button_locator = page.get_by_role("button", name="Join now")
            await join_button_locator.wait_for(timeout=15000)

            job_status[job_id] = {"status": "recording"}
            recorder = subprocess.Popen(ffmpeg_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            await join_button_locator.click(timeout=15000)

            await page.get_by_role("button", name=re.compile("Leave|Hang up|Выйти", re.IGNORECASE)).wait_for(state="visible", timeout=45000)
            print("✅ Bot has successfully joined the meeting.")
            await asyncio.sleep(5)

            while True:
                await asyncio.sleep(5)

                try:
                    if job_status.get(job_id, {}).get("status") == "stopping":
                        print("Stop signal received, leaving meeting.")
                        break

                    # Check if in lobby
                    lobby_text_pattern = re.compile("waiting for others to join|Someone in the meeting should let you in soon|Ожидание присоединения других участников", re.IGNORECASE)
                    if await page.get_by_text(lobby_text_pattern).is_visible():
                        print("🕒 Bot is in the lobby, waiting...")
                        continue

                    # --- IMPROVED PARTICIPANT COUNT LOGIC ---
                    # First, open the participants panel
                    participant_button = page.get_by_role("button", name=re.compile("People|Participants|Участники", re.IGNORECASE))
                    await participant_button.click()
                    await asyncio.sleep(2)  # Wait for panel to fully load
                    
                    await page.screenshot(path=os.path.join(output_dir, "2_after_participants_click.png"))

                    # Strategy 1: Count using the panel and listitems
                    try:
                        participants_panel = page.get_by_role("complementary", name=re.compile("Participants|People|Участники", re.IGNORECASE))
                        await participants_panel.wait_for(state="visible", timeout=10000)
                        
                        # Count all listitems in the participants panel
                        participant_items = participants_panel.get_by_role("listitem")
                        participant_count = await participant_items.count()
                        
                        print(f"👥 Found {participant_count} participant(s) in panel.")
                        
                    except Exception as e:
                        print(f"⚠️ Panel count failed: {e}, trying alternative method...")
                        
                        # Strategy 2: Try to get count from button text/aria-label
                        try:
                            button_label = await participant_button.get_attribute("aria-label")
                            if button_label:
                                match = re.search(r'(\d+)', button_label)
                                if match:
                                    participant_count = int(match.group(1))
                                    print(f"👥 Found {participant_count} participant(s) from button label.")
                                else:
                                    participant_count = 1  # Assume at least bot is there
                                    print("⚠️ Could not parse count, assuming 1 participant.")
                            else:
                                participant_count = 1
                                print("⚠️ No button label, assuming 1 participant.")
                        except Exception as e2:
                            print(f"⚠️ Button label parsing failed: {e2}")
                            participant_count = 1  # Default to 1 to prevent premature exit
                    
                    # Close the participant panel
                    await participant_button.click()
                    await asyncio.sleep(1)

                    # Exit condition: Leave only if 1 or fewer participants (only bot remains)
                    if participant_count <= 1:
                        print("⚠️ Only 1 participant (bot) left in meeting. Ending recording.")
                        break
                    else:
                        print(f"✅ {participant_count} participants still in meeting. Continuing...")

                except (TimeoutError, AttributeError) as e:
                    print(f"❌ Error checking participant count: {e}. Continuing for now...")
                    await page.screenshot(path=os.path.join(output_dir, "participant_error.png"))
                    # Don't break on error - continue monitoring
                    continue

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
