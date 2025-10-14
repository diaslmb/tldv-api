"""
debug_captions.py - Tool to debug caption scraping in Google Meet

This script helps identify the correct DOM selectors for captions.
Run it to inspect a Google Meet page structure.
"""
import asyncio
import re
from playwright.async_api import async_playwright, TimeoutError

async def debug_caption_structure(meeting_url: str):
    """
    Join a meeting and print out the caption DOM structure to help debug.
    """
    async with async_playwright() as p:
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

        print("🌐 Navigating to meeting...")
        await page.goto(meeting_url, timeout=60000)

        try:
            # --- Pre-Join Steps ---
            print("👤 Filling in name...")
            await page.locator('input[placeholder="Your name"]').fill("Debugger Bot")

            print("🔇 Turning off mic and camera...")
            await page.locator('div[data-is-muted="false"][role="button"][aria-label*="microphone"]').click(timeout=5000)
            await page.locator('div[data-is-muted="false"][role="button"][aria-label*="camera"]').click(timeout=5000)

            print("✅ Clicking 'Join now'...")
            join_button_locator = page.get_by_role("button", name=re.compile("Join now|Ask to join", re.IGNORECASE))
            await join_button_locator.click(timeout=15000)

            await page.get_by_role("button", name="Leave call").wait_for(state="visible", timeout=45000)
            print("🎉 Successfully joined the meeting.")

            # --- Enable Captions ---
            print("💬 Attempting to enable captions...")
            try:
                # Use the most reliable button selector from your main script
                caption_button = page.locator('button[jsname="r8qRAd"]').first
                await caption_button.click(timeout=10000)
                print("✅ Captions button clicked.")
            except TimeoutError:
                print("⚠️ Caption button not found with primary selector, trying keyboard shortcut...")
                await page.keyboard.press("c")


            # --- Inspect a Caption ---
            print("\n" + "="*80)
            print("👀 WAITING FOR CAPTIONS TO APPEAR...")
            print("Speak into your microphone to generate some captions.")
            print("The script will now periodically print the HTML of the caption container.")
            print("Look for attributes like 'jsname', 'data-speaker-name', or unique class names.")
            print("="*80 + "\n")

            caption_container_selector = 'div[jsname="dsyhDe"]' # A common selector for the main caption container

            while True:
                try:
                    await page.wait_for_selector(caption_container_selector, state="visible", timeout=10000)
                    caption_container = page.locator(caption_container_selector)

                    # Get the outer HTML of the container
                    html_content = await caption_container.evaluate('(element) => element.outerHTML')

                    print(f"--- CAPTION STRUCTURE AT {asyncio.get_event_loop().time():.2f} ---")
                    print(html_content)
                    print("-" * 50 + "\n")

                except TimeoutError:
                    print("... Still waiting for caption container to be visible ...")
                except Exception as e:
                    print(f"An error occurred: {e}")
                    break

                await asyncio.sleep(10) # Wait 10 seconds before checking again

        except Exception as e:
            print(f"\n❌ An error occurred during the debugging process: {e}")
            await page.screenshot(path="debug_error.png")
            print("📸 A screenshot 'debug_error.png' has been saved.")
        finally:
            print("\n👋 Press Ctrl+C to exit the script.")
            # Keep the browser open for manual inspection
            await page.pause()
            await browser.close()

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python debug_captions.py <your_google_meet_url>")
        sys.exit(1)

    meet_link = sys.argv[1]
    asyncio.run(debug_caption_structure(meet_link))
