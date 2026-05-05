"""
Authentication and CAPTCHA handling for LinkedIn using Playwright.
"""

import asyncio
import random
import os
import logging
from datetime import datetime

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from .utils import async_random_sleep, save_screenshot
from .extractors.selectors import LOGIN_FORM_SELECTORS, LOGGED_IN_INDICATORS, JOB_LOADING_INDICATORS

logger = logging.getLogger("linkedin_scraper")


class AuthManager:
    """Handles LinkedIn authentication and CAPTCHA detection using Playwright."""
    
    def __init__(self, page: Page, timeout: int = 20000):
        """
        Initialize authentication manager.
        
        Args:
            page: Playwright Page instance
            timeout: Timeout for authentication operations in milliseconds
        """
        self.page = page
        self.timeout = timeout
        self._login_attempted = False
        self._login_successful = False

    async def ensure_login(self, username: str, password: str) -> bool:
        """
        Ensure user is logged in (always required for LinkedIn scraping).

        Args:
            username: LinkedIn username
            password: LinkedIn password

        Returns:
            True if logged in successfully, False if login failed
        """
        if not self._login_attempted:
            cookies_file = os.environ.get("LINKEDIN_COOKIES_FILE", "").strip()
            li_at = os.environ.get("LINKEDIN_LI_AT", "").strip()
            if cookies_file:
                logger.info(f"🍪 Using LinkedIn cookie bundle: {cookies_file}")
                self._login_successful = await self.login_with_cookie_bundle(cookies_file)
            elif li_at:
                logger.info("🍪 Using LINKEDIN_LI_AT session cookie (skipping form login)")
                self._login_successful = await self.login_with_cookie(li_at)
            else:
                logger.info("🔐 Attempting LinkedIn login (required for job scraping)...")
                self._login_successful = await self.login(username, password)
            self._login_attempted = True

            if self._login_successful:
                logger.info("✅ Successfully logged in to LinkedIn")
            else:
                logger.error("❌ Login failed! Cannot proceed without LinkedIn authentication.")
                raise RuntimeError(
                    "LinkedIn login is required for job scraping but failed. "
                    "Please check your credentials in the .env file."
                )

        return self._login_successful

    async def login_with_cookie_bundle(self, cookies_file: str) -> bool:
        """Inject a full exported cookie bundle (Cookie-Editor / EditThisCookie JSON format)."""
        import json
        try:
            with open(cookies_file) as f:
                raw = json.load(f)
            same_site_map = {"no_restriction": "None", "lax": "Lax", "strict": "Strict",
                             "none": "None", None: "Lax", "": "Lax"}
            normalized = []
            for c in raw:
                name = c.get("name")
                if not name or name.startswith("__Secure-") and not c.get("secure"):
                    continue
                domain = c.get("domain", "")
                cookie = {
                    "name": name,
                    "value": c.get("value", ""),
                    "domain": domain,
                    "path": c.get("path", "/"),
                    "httpOnly": bool(c.get("httpOnly", False)),
                    "secure": bool(c.get("secure", False)),
                    "sameSite": same_site_map.get(c.get("sameSite"), "Lax"),
                }
                if not c.get("session") and c.get("expirationDate"):
                    cookie["expires"] = float(c["expirationDate"])
                normalized.append(cookie)
            await self.page.context.add_cookies(normalized)
            logger.info(f"✅ Injected {len(normalized)} cookies")
            await self.page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)
            await async_random_sleep(2.0, 3.5)
            # Verify via URL (not redirected to /login) + indicator selectors
            if "/login" in self.page.url or "/checkpoint" in self.page.url:
                logger.error(f"Cookie bundle rejected — redirected to {self.page.url}")
                return False
            for selector in LOGGED_IN_INDICATORS:
                try:
                    await self.page.wait_for_selector(selector, timeout=self.timeout)
                    logger.info(f"✅ Session verified via selector: {selector}")
                    return True
                except Exception:
                    continue
            # Fallback: check page title / text for logged-in markers
            title = await self.page.title()
            if title and "Feed" in title:
                logger.info(f"✅ Session verified via title: {title[:60]}")
                return True
            logger.error(f"Cookie bundle injected but no logged-in indicator found. URL={self.page.url} Title={title[:60]!r}")
            return False
        except Exception as e:
            logger.error(f"Cookie bundle login failed: {e}")
            return False

    async def login_with_cookie(self, li_at: str) -> bool:
        """Inject li_at session cookie and verify it produces a logged-in session."""
        try:
            await self.page.context.add_cookies([{
                "name": "li_at",
                "value": li_at,
                "domain": ".linkedin.com",
                "path": "/",
                "httpOnly": True,
                "secure": True,
                "sameSite": "None",
            }])
            await self.page.goto("https://www.linkedin.com/feed/")
            await async_random_sleep(2.0, 3.0)
            for selector in LOGGED_IN_INDICATORS:
                try:
                    await self.page.wait_for_selector(selector, timeout=self.timeout)
                    return True
                except Exception:
                    continue
            logger.error("Cookie injected but no logged-in indicator found — cookie may be expired")
            return False
        except Exception as e:
            logger.error(f"Cookie login failed: {e}")
            return False

    async def login(self, username: str, password: str) -> bool:
        """
        Log in to LinkedIn using credentials.

        Args:
            username: LinkedIn username
            password: LinkedIn password
            
        Returns:
            True if login successful, False otherwise
        """
        if not username or not password:
            logger.error("LinkedIn credentials are missing! Cannot proceed without authentication.")
            raise ValueError("LinkedIn username and password are required for scraping.")

        try:
            logger.info(f"🔐 Attempting to login with username: {username}")

            # Navigate to LinkedIn login page
            await self.page.goto("https://www.linkedin.com/login")
            await async_random_sleep(2.0, 3.0)            # Wait for the login form
            await self.page.wait_for_selector(LOGIN_FORM_SELECTORS["username"], timeout=self.timeout)            # Fill in username and password
            username_field = await self.page.query_selector(LOGIN_FORM_SELECTORS["username"])
            password_field = await self.page.query_selector(LOGIN_FORM_SELECTORS["password"])

            # Clear and type username
            await username_field.fill("")  # Clear the field
            # Type like a human - character by character
            for char in username:
                await username_field.type(char)
                await asyncio.sleep(random.uniform(0.05, 0.2))

            # Clear and type password
            await password_field.fill("")  # Clear the field
            for char in password:
                await password_field.type(char)
                await asyncio.sleep(random.uniform(0.05, 0.2))            # Click the login button
            login_button = await self.page.query_selector(LOGIN_FORM_SELECTORS["submit"])
            await login_button.click()

            # Wait for the login to complete
            await async_random_sleep(3.0, 5.0)

            # Check if login was successful
            for selector in LOGGED_IN_INDICATORS:
                try:
                    await self.page.wait_for_selector(selector, timeout=self.timeout)
                    logger.info("Successfully logged in to LinkedIn")
                    return True
                except PlaywrightTimeoutError:
                    continue            
            logger.error("Failed to login - could not find post-login elements")
            
            # Check for security verification
            if await self.check_for_captcha():
                logger.warning(
                    "Security verification required after login. Please check the browser."
                )

            return False

        except Exception as e:
            logger.error(f"Login failed: {str(e)}")
            return False

    async def check_for_captcha(self) -> bool:
        """
        Check if the page is showing a CAPTCHA or security verification.
        
        Returns:
            True if a CAPTCHA is detected, False otherwise
        """
        try:
            # First check: Look for job listing elements to verify we're on the results page
            job_listing_selectors = JOB_LOADING_INDICATORS

            for selector in job_listing_selectors:
                element = await self.page.query_selector(selector)
                if element:
                    logger.info("Job listings found, definitely not a CAPTCHA page")
                    return False

            # Check for visible CAPTCHA indicators
            captcha_selectors = [
                "h1:has-text('Security Verification')",
                "div:has-text('CAPTCHA')",
                "div:has-text('security check')",
                "form[action*='checkpoint']",
                "input[name='captcha']",
                "img[src*='captcha']",
                "iframe[src*='captcha'], iframe[src*='recaptcha']",
            ]

            for selector in captcha_selectors:
                elements = await self.page.query_selector_all(selector)
                for element in elements:
                    if await element.is_visible():
                        logger.warning("CAPTCHA or security verification detected and is visible")
                        await save_screenshot(
                            self.page,
                            f"captcha_confirmed_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                        )

                        # Try to get the text of the verification page
                        try:
                            body = await self.page.query_selector("body")
                            body_text = await body.text_content()
                            logger.info(f"CAPTCHA page text: {body_text[:500]}...")
                        except:
                            pass

                        return True

            # Check for LinkedIn main structure
            linkedin_main_selectors = LOGGED_IN_INDICATORS

            for selector in linkedin_main_selectors:
                element = await self.page.query_selector(selector)
                if element:
                    logger.info("LinkedIn main elements found, likely not a CAPTCHA page")
                    return False

            # Check for "No results found" message
            no_results_selectors = [
                "h1:has-text('No matching')",
                "div:has-text('No matching')",
                "p:has-text('No matching')",
            ]

            for selector in no_results_selectors:
                elements = await self.page.query_selector_all(selector)
                for element in elements:
                    if await element.is_visible():
                        logger.info("'No results' message found, not a CAPTCHA page")
                        return False

            # Check page title
            page_title = await self.page.title()
            logger.info(f"Current page title: {page_title}")

            if "Security Verification" in page_title or "CAPTCHA" in page_title:
                logger.warning("CAPTCHA detected from page title")
                await save_screenshot(
                    self.page,
                    f"captcha_from_title_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                )
                return True

            return False

        except Exception as e:
            logger.error(f"Error checking for CAPTCHA: {str(e)}")
            return False
