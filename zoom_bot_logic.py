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

def get_pulse_audio_source():
    """Finds the correct PulseAudio monitor source for system audio recording."""
    try:
        result = subprocess.run(
            ["pactl", "list", "sources", "short"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
                if '.monitor' in line:
                    source_name = line.split('\t')[1]
                    print(f"🔊 Found PulseAudio monitor source: {source_name}")
                    return source_name
    except Exception as e:
        print(f"⚠️ Could not auto-detect PulseAudio source: {e}. Falling back to 'default'.")
    return "default"

def get_ffmpeg_command(platform, duration, output_path):
    if platform.startswith("linux"):
        pulse_source = get_pulse_audio_source()
        return [
            "ffmpeg", "-y", "-f", "pulse", "-i", pulse_source,
            "-ac", "2", "-ar", "44100",
            "-t", str(duration), output_path
        ]
    return None

def extract_meeting_details(url: str):
    match = re.search(r'/j/(\d+)\?pwd=([\w.-]+)', url)
    if match:
        return match.group(1), match.group(2)
    return None, None

def transcribe_audio(audio_path, transcript_path):
    if not os.path.exists(audio_path) or os.path.getsize(audio_path) < 4096:
        print(f"❌ Audio file is missing or too small. Skipping transcription.")
        return False
    # ... (rest of the function is the same)
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

    meeting_id, pwd = extract_meeting_details(meeting_url)
    if not meeting_id or not pwd:
        job_status[job_id] = {"status": "failed", "error": "Could not extract Meeting ID and/or Password from URL."}
        return

    job_status[job_id] = {"status": "starting_browser"}
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
            context = await browser.new_context(permissions=["microphone", "camera"])
            page = await context.new_page()
        except Exception as e:
            job_status[job_id] = {"status": "failed", "error": f"Failed to launch browser: {e}"}
            return

        screenshot_count = 0
        async def snap(name: str):
            nonlocal screenshot_count
            screenshot_count += 1
            path = os.path.join(output_dir, f"{screenshot_count:02d}_{name}.png")
            try:
                await page.screenshot(path=path)
                print(f"📸 Saved screenshot: {path}")
            except Exception as e:
                print(f"⚠️ Could not save screenshot {path}: {e}")

        recorder = None
        frame = None
        try:
            job_status[job_id] = {"status": "navigating"}
            await page.goto("https://app.zoom.us/wc/join", timeout=60000)
            await snap("01_navigated_to_join_page")
            
            meeting_id_placeholder = re.compile("Meeting ID|Идентификатор конференции", re.IGNORECASE)
            await page.get_by_placeholder(meeting_id_placeholder).fill(meeting_id)
            join_button_text = re.compile("Join|Подключиться", re.IGNORECASE)
            await page.get_by_role("button", name=join_button_text).click()
            
            # --- FIX: Handle Cookie Consent Dialog ---
            try:
                # This button appears on the main page, not in the iframe.
                accept_cookies_button = page.get_by_role("button", name="Accept Cookies")
                await accept_cookies_button.wait_for(state="visible", timeout=10000)
                await accept_cookies_button.click()
                print("✅ Accepted cookies.")
                await snap("02_cookies_accepted")
            except TimeoutError:
                print("ℹ️ Cookie consent dialog did not appear.")

            frame = page.frame_locator('iframe').first
            await frame.locator('body').wait_for(timeout=30000)
            await snap("03_pre_join_iframe_ready")
            
            await frame.get_by_role("button", name=re.compile("Mute|Выключить звук", re.I)).click(timeout=10000)
            await frame.get_by_role("button", name=re.compile("Stop Video|Остановить видео", re.I)).click(timeout=10000)
            await snap("04_mic_and_video_off")

            await frame.locator('input[type="password"], input[type="text"]').first.focus()
            await page.keyboard.type(pwd)
            await page.keyboard.press("Tab")
            await page.keyboard.type("SHAI AI Notetaker")
            await page.keyboard.press("Tab")
            await page.keyboard.press("Tab")
            await page.keyboard.press("Enter")
            await snap("05_form_submitted")

            await frame.get_by_role("button", name=re.compile("Leave|Завершение", re.I)).wait_for(state="visible", timeout=60000)
            await snap("06_in_meeting")
            
            job_status[job_id] = {"status": "recording"}
            recorder = subprocess.Popen(ffmpeg_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            await asyncio.sleep(2)
            if recorder.poll() is not None:
                _, stderr = recorder.communicate()
                raise Exception(f"ffmpeg failed to start: {stderr.decode()}")
            print("✅ Audio recording started.")
            
            await page.evaluate("""() => {
                const mediaElements = document.querySelectorAll('video, audio');
                for (const el of mediaElements) {
                    el.muted = false;
                    el.volume = 1.0;
                }
            }""")
            print("🔊 Ensured browser audio is unmuted.")
            await snap("07_audio_unmuted")

            while True:
                await asyncio.sleep(5)
                if job_status.get(job_id, {}).get("status") == "stopping":
                    print("Stop signal received, leaving meeting.")
                    break
                try:
                    participants_button = frame.get_by_role("button", name=re.compile("Participants|Участники", re.I))
                    count_text = await participants_button.get_attribute("aria-label") or ""
                    match = re.search(r'\d+', count_text)
                    if match and int(match.group()) <= 1:
                        await snap("08_only_one_participant_left")
                        break
                except Exception:
                    await snap("09_participants_button_not_found")
                    break
        except Exception as e:
            job_status[job_id] = {"status": "failed", "error": f"An error occurred in the meeting: {e}"}
            await snap("10_error_occurred")
        finally:
            if recorder and recorder.poll() is None:
                recorder.terminate()
                recorder.communicate()
            
            try:
                if frame:
                    await snap("11_attempting_to_leave")
                    leave_btn_text = re.compile("^Leave$|^Завершение$", re.I)
                    await frame.get_by_role("button", name=leave_btn_text).click(timeout=5000)
                    await snap("12_leave_button_clicked")
                    
                    try:
                        confirm_btn_text = re.compile("Leave Meeting|Выйти из конференции", re.I)
                        await page.get_by_role("button", name=confirm_btn_text).click(timeout=3000)
                        print("✅ Confirmed leaving meeting.")
                    except TimeoutError:
                        print("ℹ️ No leave confirmation dialog appeared, or it was not needed.")
            except Exception as e:
                print(f"Could not click leave button: {e}")
            await browser.close()

    if os.path.exists(output_audio_path) and os.path.getsize(output_audio_path) > 4096:
        job_status[job_id] = {"status": "transcribing"}
        # ... (rest of the transcription/summarization logic remains the same)
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
