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
            print(f"🌐 Navigating to: {meeting_url}")
            
            # Use domcontentloaded instead of load for faster navigation
            try:
                await page.goto(meeting_url, wait_until="domcontentloaded", timeout=90000)
                print("✅ Page loaded (domcontentloaded)")
            except TimeoutError:
                print("⚠️ Page load timeout, but continuing...")
                # Take screenshot to see what's on the page
                await page.screenshot(path=os.path.join(output_dir, "0_timeout_state.png"))

            # Wait for page to settle
            await asyncio.sleep(5)
            await page.screenshot(path=os.path.join(output_dir, "1_initial_page.png"))
            print(f"📸 Screenshot saved: 1_initial_page.png")

            # Debug: Print page title and URL
            page_title = await page.title()
            current_url = page.url
            print(f"📄 Page title: {page_title}")
            print(f"🔗 Current URL: {current_url}")

            # Handle multiple possible Zoom scenarios
            
            # Scenario 1: "Open Zoom Meetings?" dialog
            try:
                print("🔍 Checking for app launch dialog...")
                launch_dialog = page.locator('text="Open Zoom Meetings?"')
                if await launch_dialog.is_visible(timeout=5000):
                    print("Found 'Open Zoom Meetings?' dialog")
                    # Click "Cancel" or "Join from Your Browser"
                    cancel_btn = page.get_by_role("button", name=re.compile("Cancel", re.IGNORECASE))
                    await cancel_btn.click(timeout=3000)
                    print("✅ Clicked Cancel on app launch dialog")
                    await asyncio.sleep(2)
            except TimeoutError:
                print("No app launch dialog found (timeout)")
            except Exception as e:
                print(f"App launch dialog handling: {e}")

            # Scenario 2: "Join from Your Browser" link
            try:
                print("🔍 Looking for 'Join from Your Browser' link...")
                await page.screenshot(path=os.path.join(output_dir, "1b_before_browser_join.png"))
                
                # Try multiple selectors
                browser_join_selectors = [
                    'a:has-text("Join from your browser")',
                    'a:has-text("join from browser")',
                    'text="Join from Your Browser"',
                    '[href*="wc/join"]'
                ]
                
                clicked = False
                for selector in browser_join_selectors:
                    try:
                        join_link = page.locator(selector).first
                        if await join_link.is_visible(timeout=3000):
                            await join_link.click()
                            print(f"✅ Clicked browser join link (selector: {selector})")
                            clicked = True
                            await asyncio.sleep(5)
                            break
                    except:
                        continue
                
                if not clicked:
                    print("⚠️ Could not find 'Join from Your Browser' link, checking if already on join page...")
                    
            except Exception as e:
                print(f"Browser join link handling: {e}")

            await page.screenshot(path=os.path.join(output_dir, "2_after_navigation.png"))

            # Scenario 3: Check if we're on the join page
            try:
                print("🔍 Checking for join page elements...")
                page_content = await page.content()
                
                # Save HTML for debugging
                with open(os.path.join(output_dir, "page_content.html"), "w", encoding="utf-8") as f:
                    f.write(page_content)
                print("📝 Saved page HTML for debugging")
                
            except Exception as e:
                print(f"Debug content save failed: {e}")

            # Fill in the name
            try:
                print("🔍 Looking for name input field...")
                # Try multiple possible selectors for name input
                name_input = None
                name_selectors = [
                    'input[type="text"]',
                    'input[placeholder*="name" i]',
                    'input#inputname',
                    'input[aria-label*="name" i]'
                ]
                
                for selector in name_selectors:
                    try:
                        input_field = page.locator(selector).first
                        if await input_field.is_visible(timeout=5000):
                            name_input = input_field
                            print(f"✅ Found name input with selector: {selector}")
                            break
                    except:
                        continue
                
                if name_input:
                    await name_input.fill("SHAI AI Notetaker")
                    print("✅ Filled in name")
                else:
                    print("⚠️ Could not find name input field")
                    
            except Exception as e:
                print(f"Name input error: {e}")

            await page.screenshot(path=os.path.join(output_dir, "3_pre_join_screen.png"))

            # Turn off audio/video
            try:
                print("🎤 Attempting to turn off microphone and camera...")
                
                # Turn off audio checkbox if present
                try:
                    audio_checkbox = page.locator('input[type="checkbox"][id*="audio"]').first
                    if await audio_checkbox.is_visible(timeout=3000):
                        is_checked = await audio_checkbox.is_checked()
                        if is_checked:
                            await audio_checkbox.click()
                            print("✅ Unchecked audio checkbox")
                except Exception as e:
                    print(f"Audio checkbox: {e}")

                # Click mute button
                try:
                    # Zoom uses specific aria-labels
                    mute_selectors = [
                        'button[aria-label*="Mute" i]',
                        'button[aria-label*="mute my microphone" i]',
                        'button:has-text("Mute")'
                    ]
                    
                    for selector in mute_selectors:
                        try:
                            mute_btn = page.locator(selector).first
                            if await mute_btn.is_visible(timeout=2000):
                                await mute_btn.click()
                                print(f"✅ Clicked mute button ({selector})")
                                break
                        except:
                            continue
                            
                except Exception as e:
                    print(f"Mute button: {e}")

                # Turn off video
                try:
                    video_selectors = [
                        'button[aria-label*="video" i]',
                        'button[aria-label*="camera" i]',
                        'button:has-text("Stop Video")'
                    ]
                    
                    for selector in video_selectors:
                        try:
                            video_btn = page.locator(selector).first
                            if await video_btn.is_visible(timeout=2000):
                                await video_btn.click()
                                print(f"✅ Clicked video button ({selector})")
                                break
                        except:
                            continue
                            
                except Exception as e:
                    print(f"Video button: {e}")

                await asyncio.sleep(1)
                await page.screenshot(path=os.path.join(output_dir, "4_after_mute.png"))

            except Exception as e:
                print(f"Audio/video toggle error: {e}")

            # Click Join button
            try:
                print("🔍 Looking for Join button...")
                join_selectors = [
                    'button:has-text("Join")',
                    'button[aria-label*="Join" i]',
                    'button#joinBtn',
                    'input[type="submit"][value="Join"]'
                ]
                
                join_button = None
                for selector in join_selectors:
                    try:
                        btn = page.locator(selector).first
                        if await btn.is_visible(timeout=5000):
                            join_button = btn
                            print(f"✅ Found Join button ({selector})")
                            break
                    except:
                        continue
                
                if not join_button:
                    job_status[job_id] = {"status": "failed", "error": "Could not find Join button"}
                    await page.screenshot(path=os.path.join(output_dir, "error_no_join_button.png"))
                    return
                
                job_status[job_id] = {"status": "recording"}
                recorder = subprocess.Popen(ffmpeg_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                
                await join_button.click()
                print("✅ Clicked Join button")
                
            except Exception as e:
                job_status[job_id] = {"status": "failed", "error": f"Join button error: {e}"}
                return

            # Wait for meeting to load
            print("⏳ Waiting for meeting to load...")
            await asyncio.sleep(10)
            
            await page.screenshot(path=os.path.join(output_dir, "5_in_meeting.png"))
            
            # Verify we're in the meeting
            try:
                # Look for meeting controls
                controls_found = False
                control_selectors = [
                    'button[aria-label*="Participants" i]',
                    'button[aria-label*="Leave" i]',
                    'button:has-text("Participants")',
                    'button:has-text("Leave")'
                ]
                
                for selector in control_selectors:
                    try:
                        if await page.locator(selector).first.is_visible(timeout=5000):
                            controls_found = True
                            print(f"✅ Found meeting control: {selector}")
                            break
                    except:
                        continue
                
                if controls_found:
                    print("✅ Bot has successfully joined the meeting")
                else:
                    print("⚠️ Could not confirm meeting join, but continuing...")
                    
            except Exception as e:
                print(f"Meeting verification: {e}")

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
                        # Find Participants button
                        participants_button = None
                        participant_selectors = [
                            'button[aria-label*="Participants" i]',
                            'button:has-text("Participants")',
                            'button[aria-label*="manage participants" i]'
                        ]
                        
                        for selector in participant_selectors:
                            try:
                                btn = page.locator(selector).first
                                if await btn.is_visible(timeout=3000):
                                    participants_button = btn
                                    break
                            except:
                                continue
                        
                        if participants_button:
                            # Try to get count from button text
                            try:
                                button_text = await participants_button.inner_text()
                                match = re.search(r'\((\d+)\)|\b(\d+)\b', button_text)
                                if match:
                                    count_str = match.group(1) or match.group(2)
                                    participant_count = int(count_str)
                                    print(f"👥 Found {participant_count} participant(s)")
                            except:
                                pass
                            
                            # If still default, open panel and count
                            if participant_count == 2:
                                try:
                                    await participants_button.click()
                                    await asyncio.sleep(2)
                                    await page.screenshot(path=os.path.join(output_dir, f"participants_{int(asyncio.get_event_loop().time())}.png"))
                                    
                                    # Count items in participants list
                                    participant_items = page.locator('[class*="participants-item"], [class*="participant-list-item"]')
                                    count = await participant_items.count()
                                    if count > 0:
                                        participant_count = count
                                        print(f"👥 Counted {participant_count} participant items")
                                    
                                    # Close panel
                                    await participants_button.click()
                                    await asyncio.sleep(1)
                                except:
                                    pass
                        else:
                            print("⚠️ Participants button not visible")
                            
                    except Exception as e:
                        print(f"⚠️ Error checking participants: {e}")

                    # Exit if only bot remains
                    if participant_count <= 1:
                        print(f"🚪 Only {participant_count} participant(s) remaining. Leaving meeting.")
                        break
                    else:
                        print(f"✅ {participant_count} participants in meeting. Monitoring...")

                except Exception as e:
                    print(f"❌ Error in monitoring loop: {e}")
                    await page.screenshot(path=os.path.join(output_dir, "monitoring_error.png"))

        except Exception as e:
            job_status[job_id] = {"status": "failed", "error": f"An error occurred: {e}"}
            await page.screenshot(path=os.path.join(output_dir, "error.png"))
            print(f"❌ Fatal error: {e}")
        finally:
            if recorder and recorder.poll() is None:
                recorder.terminate()
                recorder.communicate()

            try:
                # Click Leave button
                leave_selectors = [
                    'button[aria-label*="Leave" i]',
                    'button:has-text("Leave")',
                    'button:has-text("End")'
                ]
                
                for selector in leave_selectors:
                    try:
                        leave_btn = page.locator(selector).first
                        if await leave_btn.is_visible(timeout=3000):
                            await leave_btn.click()
                            print("✅ Clicked Leave button")
                            await asyncio.sleep(2)
                            
                            # Confirm if needed
                            confirm_selectors = [
                                'button:has-text("Leave Meeting")',
                                'button:has-text("Yes")',
                                'button:has-text("Leave")'
                            ]
                            
                            for confirm_sel in confirm_selectors:
                                try:
                                    confirm_btn = page.locator(confirm_sel).first
                                    if await confirm_btn.is_visible(timeout=2000):
                                        await confirm_btn.click()
                                        print("✅ Confirmed leave")
                                        break
                                except:
                                    continue
                            break
                    except:
                        continue
                        
            except Exception as e:
                print(f"Leave button error: {e}")

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
