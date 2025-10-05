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
            browser = await p.chromium.launch(
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--use-fake-ui-for-media-stream",
                    "--use-fake-device-for-media-stream"
                ]
            )
            context = await browser.new_context(permissions=["microphone", "camera"])
            page = await context.new_page()
        except Exception as e:
            job_status[job_id] = {"status": "failed", "error": f"Failed to launch browser: {e}"}
            return

        recorder = None
        try:
            job_status[job_id] = {"status": "navigating"}
            await page.goto(meeting_url, timeout=60000)

            # Wait for the page to load
            await asyncio.sleep(3)
            await page.screenshot(path=os.path.join(output_dir, "1_initial_page.png"))

            # Handle "Open Zoom Meetings?" dialog - click "Cancel" to use web client
            try:
                cancel_button = page.get_by_role("button", name=re.compile("Cancel|Launch Meeting", re.IGNORECASE))
                if await cancel_button.is_visible(timeout=5000):
                    await cancel_button.click()
                    print("✅ Dismissed app launch dialog")
                    await asyncio.sleep(2)
            except TimeoutError:
                print("No app launch dialog found")

            # Click "Join from Your Browser" if it appears
            try:
                join_browser_link = page.get_by_role("link", name=re.compile("Join from your browser|join from browser", re.IGNORECASE))
                if await join_browser_link.is_visible(timeout=5000):
                    await join_browser_link.click()
                    print("✅ Clicked 'Join from Your Browser'")
                    await asyncio.sleep(3)
            except TimeoutError:
                print("No 'Join from browser' link found")

            # Fill in the name
            try:
                name_input = page.locator('input[type="text"]').first
                await name_input.wait_for(state="visible", timeout=15000)
                await name_input.fill("SHAI AI Notetaker")
                print("✅ Filled in name")
            except TimeoutError:
                print("⚠️ Could not find name input field")

            await page.screenshot(path=os.path.join(output_dir, "2_pre_join_screen.png"))

            # Turn off video and audio before joining
            try:
                print("🎤 Turning off microphone and camera...")
                
                # Uncheck "Join with Computer Audio" checkbox if present
                try:
                    audio_checkbox = page.locator('input[type="checkbox"]').first
                    if await audio_checkbox.is_checked():
                        await audio_checkbox.uncheck()
                        print("✅ Unchecked audio checkbox")
                except Exception as e:
                    print(f"Audio checkbox handling: {e}")

                # Turn off microphone using button
                try:
                    mic_button = page.get_by_role("button", name=re.compile("mute|microphone", re.IGNORECASE)).first
                    await mic_button.click(timeout=3000)
                    print("✅ Clicked microphone button")
                except Exception as e:
                    print(f"Mic button click failed: {e}")

                # Turn off video using button
                try:
                    video_button = page.get_by_role("button", name=re.compile("video|camera", re.IGNORECASE)).first
                    await video_button.click(timeout=3000)
                    print("✅ Clicked video button")
                except Exception as e:
                    print(f"Video button click failed: {e}")

                await asyncio.sleep(1)
                await page.screenshot(path=os.path.join(output_dir, "3_after_mute.png"))

            except Exception as e:
                print(f"⚠️ Error toggling audio/video: {e}")

            # Click "Join" button
            try:
                join_button = page.get_by_role("button", name=re.compile("^Join$|Join Meeting", re.IGNORECASE))
                await join_button.wait_for(state="visible", timeout=15000)
                
                job_status[job_id] = {"status": "recording"}
                recorder = subprocess.Popen(ffmpeg_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                
                await join_button.click()
                print("✅ Clicked Join button")
            except TimeoutError:
                job_status[job_id] = {"status": "failed", "error": "Could not find Join button"}
                return

            # Wait for meeting to fully load
            await asyncio.sleep(8)
            
            # Wait for participants panel or meeting controls to appear
            try:
                # Look for participants button or leave button to confirm we're in the meeting
                await page.get_by_role("button", name=re.compile("Participants|Leave", re.IGNORECASE)).first.wait_for(state="visible", timeout=30000)
                print("✅ Bot has successfully joined the meeting")
            except TimeoutError:
                print("⚠️ Could not confirm meeting join, but continuing...")

            await page.screenshot(path=os.path.join(output_dir, "4_in_meeting.png"))

            # Main monitoring loop
            while True:
                await asyncio.sleep(5)

                try:
                    if job_status.get(job_id, {}).get("status") == "stopping":
                        print("Stop signal received, leaving meeting.")
                        break

                    # Check participant count
                    participant_count = 2  # Safe default

                    try:
                        # Find and click the Participants button
                        participants_button = page.get_by_role("button", name=re.compile("Participants", re.IGNORECASE)).first
                        
                        if await participants_button.is_visible(timeout=3000):
                            # Check if panel is already open by looking for the panel
                            panel_open = await page.locator('[class*="participants"]').first.is_visible()
                            
                            if not panel_open:
                                await participants_button.click()
                                await asyncio.sleep(2)
                                print("Opened participants panel")
                            
                            await page.screenshot(path=os.path.join(output_dir, f"participants_{int(asyncio.get_event_loop().time())}.png"))
                            
                            # Try to get participant count from the button text
                            try:
                                button_text = await participants_button.inner_text()
                                # Look for pattern like "Participants (3)" or just a number
                                match = re.search(r'\((\d+)\)|\b(\d+)\b', button_text)
                                if match:
                                    count_str = match.group(1) or match.group(2)
                                    participant_count = int(count_str)
                                    print(f"👥 Found {participant_count} participant(s) from button text")
                            except Exception as e:
                                print(f"Could not parse button text: {e}")
                            
                            # Alternative: Count participant items in the panel
                            if participant_count == 2:  # Still default
                                try:
                                    # Look for participant list items
                                    participant_items = page.locator('[class*="participants-item"], [class*="participant-item"]')
                                    count = await participant_items.count()
                                    if count > 0:
                                        participant_count = count
                                        print(f"👥 Counted {participant_count} participant items in panel")
                                except Exception as e:
                                    print(f"Could not count participant items: {e}")
                            
                            # Close panel if we opened it
                            if not panel_open and await participants_button.is_visible():
                                await participants_button.click()
                                await asyncio.sleep(1)
                        else:
                            print("⚠️ Participants button not visible")
                            
                    except Exception as e:
                        print(f"⚠️ Error checking participants: {e}")

                    # Exit if only bot remains
                    if participant_count <= 1:
                        print(f"🚪 Only {participant_count} participant(s) remaining. Bot is alone. Leaving meeting.")
                        break
                    else:
                        print(f"✅ {participant_count} participants in meeting. Monitoring...")

                except Exception as e:
                    print(f"❌ Error in monitoring loop: {e}")
                    await page.screenshot(path=os.path.join(output_dir, "monitoring_error.png"))

        except Exception as e:
            job_status[job_id] = {"status": "failed", "error": f"An error occurred: {e}"}
            await page.screenshot(path=os.path.join(output_dir, "error.png"))
        finally:
            if recorder and recorder.poll() is None:
                recorder.terminate()
                recorder.communicate()

            try:
                # Click Leave button
                leave_button = page.get_by_role("button", name=re.compile("Leave|End", re.IGNORECASE)).first
                await leave_button.click(timeout=5000)
                await asyncio.sleep(1)
                
                # Confirm leave if needed
                try:
                    confirm_leave = page.get_by_role("button", name=re.compile("Leave Meeting|Yes", re.IGNORECASE))
                    await confirm_leave.click(timeout=3000)
                except:
                    pass
                    
                await asyncio.sleep(2)
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
