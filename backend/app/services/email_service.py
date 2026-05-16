"""
Email ingestion service for IMAP-based document processing.

Provides EmailIngestionService class that periodically polls an IMAP inbox,
extracts document attachments, and enqueues them for processing via BackgroundProcessor.
Supports vault routing via subject tags [VaultName] or #vaultname.
"""

import asyncio
import email
import email.policy
import logging
import os
import re
import tempfile
from datetime import datetime
from email.header import decode_header
from email.message import EmailMessage
from typing import Optional, Union

import aioimaplib
import bleach

from app.config import Settings
from app.models.database import SQLiteConnectionPool
from app.services.background_tasks import BackgroundProcessor
from app.services.upload_path import UploadPathProvider

logger = logging.getLogger(__name__)

# Allowed HTML tags for sanitization
ALLOWED_TAGS = ['p', 'br', 'strong', 'em', 'ul', 'ol', 'li']

# Maximum email size limit (50MB)
MAX_EMAIL_SIZE = 50 * 1024 * 1024  # 50MB in bytes

# Maximum number of attachments per email (default 10)
MAX_ATTACHMENTS_PER_EMAIL = 10


class EmailIngestionService:
    """
    Email ingestion service for IMAP-based document processing.

    Periodically polls an IMAP mailbox for UNSEEN emails, extracts document
    attachments, and enqueues them for processing via BackgroundProcessor.
    Supports vault routing via subject tags [VaultName] or #vaultname.

    Attributes:
        settings: Settings instance with IMAP configuration
        pool: SQLiteConnectionPool for database access
        background_processor: BackgroundProcessor for document processing
        _stop_event: asyncio.Event for graceful shutdown
        _polling_task: Reference to the polling coroutine
        _running: Boolean indicating if service is active
        _last_error: Last error message (for health check)
    """

    def __init__(
        self,
        settings: Settings,
        pool: SQLiteConnectionPool,
        background_processor: BackgroundProcessor,
    ):
        """
        Initialize the email ingestion service.

        Args:
            settings: Settings instance with IMAP configuration
            pool: SQLiteConnectionPool for database access
            background_processor: BackgroundProcessor for document processing
        """
        self.settings = settings
        self.pool = pool
        self.background_processor = background_processor
        self._stop_event = asyncio.Event()
        self._polling_task: Optional[asyncio.Task] = None
        self._running = False
        self._last_error: Optional[str] = None
        self._last_poll_time: Optional[datetime] = None
        self._current_backoff_delay: Optional[int] = None

    async def start_polling(self) -> None:
        """
        Start the IMAP polling loop.

        Begins polling the IMAP mailbox at configured intervals if imap_enabled.
        Safe to call multiple times - will not create duplicate pollers.
        """
        if self._running:
            logger.warning("Email ingestion service is already running")
            return

        if not self.settings.imap_enabled:
            logger.info("IMAP email ingestion is disabled, service not started")
            return

        self._running = True
        self._stop_event.clear()
        self._polling_task = asyncio.create_task(self._polling_loop())
        self._polling_task.add_done_callback(self._on_polling_task_done)
        logger.info("Email ingestion service started")

    def _on_polling_task_done(self, task: asyncio.Task) -> None:
        """Callback invoked when the polling task finishes — logs unhandled exceptions."""
        if task.cancelled():
            logger.info("Email polling task was cancelled")
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "Email polling task exited with an unhandled exception: %s",
                exc,
                exc_info=exc,
            )
            self._running = False

    def stop_polling(self) -> None:
        """
        Stop the email ingestion service gracefully.

        Signals the service to shut down. The actual stop is handled
        asynchronously in the polling loop.
        """
        if not self._running:
            logger.warning("Email ingestion service is not running")
            return

        logger.info("Stopping email ingestion service...")
        self._stop_event.set()

    def is_healthy(self) -> bool:
        """
        Check if the email ingestion service is healthy.

        Returns:
            True if the service is running without recent errors, False otherwise
        """
        return self._running and self._last_error is None

    def get_last_poll_time(self) -> Optional[datetime]:
        """
        Get the timestamp of the last successful poll.

        Returns:
            Datetime of last poll, or None if no poll has completed
        """
        return self._last_poll_time

    def get_current_backoff_delay(self) -> Optional[int]:
        """
        Get the current backoff delay in seconds.

        Returns:
            Current backoff delay in seconds, or None if not in backoff
        """
        return self._current_backoff_delay

    async def _polling_loop(self) -> None:
        """
        Main polling loop that periodically checks for new emails.

        Continuously polls at configured intervals until stop_event is set.
        Handles exceptions gracefully to keep the service running.
        """
        interval_seconds = self.settings.imap_poll_interval

        while not self._stop_event.is_set():
            try:
                await self._poll_once()
                # Clear last error on successful poll
                self._last_error = None
            except asyncio.CancelledError:
                logger.info("Polling loop cancelled")
                break
            except (OSError, RuntimeError, ConnectionError) as e:
                self._last_error = str(e)
                logger.error(f"Error during email polling: {e}", exc_info=True)
            except Exception as e:
                self._last_error = str(e)
                logger.error(
                    "Unexpected error in email polling loop (stopping service): %s",
                    e,
                    exc_info=True,
                )
                break

            # Wait for next poll interval or shutdown
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=interval_seconds
                )
            except asyncio.TimeoutError:
                # Timeout means interval elapsed, continue to next poll
                pass

        self._running = False
        logger.info("Email ingestion service stopped")

    async def _poll_once(self) -> None:
        """
        Single poll iteration: connect, process emails, mark seen.

        1. Connect to IMAP with backoff
        2. Search for UNSEEN emails
        3. Process each email
        4. Mark emails as seen

        Raises:
            Exception: If connection or processing fails
        """
        imap_client = None
        try:
            imap_client = await self._connect_with_backoff()

            # Select mailbox
            await imap_client.select(self.settings.imap_mailbox)
            logger.debug(f"Selected mailbox: {self.settings.imap_mailbox}")

            # Search for UNSEEN emails
            result, data = await imap_client.search(None, 'UNSEEN')
            if result != 'OK':
                logger.warning(f"IMAP search failed: {result}")
                return

            uids = data[0].split()
            logger.info(f"Found {len(uids)} UNSEEN emails")

            # Process each email
            for uid in uids:
                uid_str = uid.decode() if isinstance(uid, bytes) else str(uid)
                try:
                    await self._process_email(imap_client, uid_str)
                except (OSError, RuntimeError, ValueError) as e:
                    logger.error(f"Error processing email UID {uid_str}: {e}", exc_info=True)
                    # Continue to next email even if one fails

            # Update last poll time on successful completion
            self._last_poll_time = datetime.now()

        except (OSError, RuntimeError, ConnectionError) as e:
            logger.error(f"Error during poll iteration: {e}", exc_info=True)
            raise
        finally:
            if imap_client:
                try:
                    await imap_client.logout()
                    logger.debug("IMAP connection closed")
                except (OSError, ConnectionError) as e:
                    logger.warning(f"Error closing IMAP connection: {e}")

    async def _connect_with_backoff(self) -> Union[aioimaplib.IMAP4_SSL, aioimaplib.IMAP4]:
        """
        Connect to IMAP server with exponential backoff.

        Implements exponential backoff: 5s → 15s → 30s → 60s (max)
        Handles authentication errors specially (logs and raises).

        Returns:
            Connected aioimaplib.IMAP4_SSL client

        Raises:
            Exception: If connection fails after all retries or auth fails
        """
        max_delay = 60
        delay = 5
        attempts = 0

        while not self._stop_event.is_set():
            attempts += 1
            # Track current backoff delay for status endpoint
            self._current_backoff_delay = delay if attempts > 1 else None
            try:
                logger.debug(
                    f"Connecting to IMAP server {self.settings.imap_host}:"
                    f"{self.settings.imap_port} (attempt {attempts})"
                )

                if self.settings.imap_use_ssl:
                    imap_client = aioimaplib.IMAP4_SSL(
                        host=self.settings.imap_host,
                        port=self.settings.imap_port,
                        timeout=30
                    )
                else:
                    imap_client = aioimaplib.IMAP4(
                        host=self.settings.imap_host,
                        port=self.settings.imap_port,
                        timeout=30
                    )

                # Authenticate (never log password)
                result = await imap_client.wait_hello_from_server()
                if result != 'OK':
                    raise Exception(f"IMAP server greeting failed: {result}")

                result, _ = await imap_client.login(
                    self.settings.imap_username,
                    self.settings.imap_password.get_secret_value()
                )
                if result != 'OK':
                    # Auth error is permanent, don't retry with backoff
                    error_msg = "IMAP authentication failed: check username/password"
                    logger.error(error_msg)
                    raise Exception(error_msg)

                logger.info(f"IMAP connection established after {attempts} attempt(s)")
                # Clear backoff delay on successful connection
                self._current_backoff_delay = None
                return imap_client

            except (OSError, ConnectionError, RuntimeError, TimeoutError) as e:
                if "authentication" in str(e).lower():
                    # Auth errors are permanent, don't retry
                    raise

                logger.warning(
                    f"IMAP connection attempt {attempts} failed: {e}, "
                    f"retrying in {delay}s..."
                )

                if delay >= max_delay:
                    # Max delay reached, give up
                    error_msg = f"IMAP connection failed after {attempts} attempts"
                    logger.error(error_msg)
                    raise Exception(error_msg)

                # Wait for backoff delay or shutdown
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass

                # Exponential backoff: 5s → 15s → 30s → 60s (max)
                delay = min(delay * 3, max_delay)

        # Stop event was set
        raise Exception("Email ingestion service stopped during connection attempt")

    async def _process_email(self, imap_client: aioimaplib.IMAP4_SSL, uid: str) -> None:
        """
        Process a single email: extract content, save attachments, enqueue.

        Fetches email content, parses with email.message_from_bytes(),
        extracts subject and sender, resolves vault from subject tag,
        extracts and validates attachments, saves to temp files,
        and queues via background_processor.enqueue().

        Args:
            imap_client: Connected IMAP client
            uid: Email UID to process

        Raises:
            Exception: If email processing fails
        """
        # Check email size before fetching to prevent DoS
        result, data = await imap_client.fetch(uid, '(RFC822.SIZE)')
        if result != 'OK' or not data[0]:
            logger.warning(f"Failed to fetch email size for UID {uid}")
            return

        # Extract size from response (format: "123 (RFC822.SIZE {size})")
        size_match = re.search(r'RFC822\.SIZE (\d+)', str(data[0]))
        if size_match:
            email_size = int(size_match.group(1))
            if email_size > MAX_EMAIL_SIZE:
                size_mb = email_size / (1024 * 1024)
                max_mb = MAX_EMAIL_SIZE / (1024 * 1024)
                logger.warning(
                    f"Email UID {uid} too large ({size_mb:.2f}MB > {max_mb:.2f}MB), skipping"
                )
                return

        # Fetch email content
        result, data = await imap_client.fetch(uid, '(RFC822)')
        if result != 'OK' or not data[0]:
            logger.warning(f"Failed to fetch email UID {uid}")
            return

        raw_email = data[0][1]  # Extract email bytes
        msg: EmailMessage = email.message_from_bytes(raw_email, policy=email.policy.default)

        # Extract subject and sender
        subject = self._decode_header_value(msg.get('Subject', ''))
        sender = self._decode_header_value(msg.get('From', ''))

        # Use sanitized values for logging to prevent log injection
        logger.info(f"Processing email from {self._sanitize_log_value(sender)}: {self._sanitize_log_value(subject)}")

        # Extract vault name from subject tag [VaultName] or #vaultname
        vault_name = self._extract_vault_name(subject)
        vault_id = await self._resolve_vault_id(vault_name)
        logger.debug(f"Resolved vault_id={vault_id} for vault_name={vault_name}")

        # Extract and process attachments
        processed_attachments = 0
        for part in msg.walk():
            # Skip non-attachment parts
            if part.get_content_disposition() != 'attachment':
                continue

            # Check attachment count limit
            if processed_attachments >= MAX_ATTACHMENTS_PER_EMAIL:
                logger.warning(
                    f"Email has more than {MAX_ATTACHMENTS_PER_EMAIL} attachments, "
                    f"skipping remaining attachments"
                )
                break

            # Validate attachment
            is_valid, reason = self._validate_attachment(part)
            if not is_valid:
                logger.debug(f"Skipping attachment: {reason}")
                continue

            # Save attachment to temp file
            file_path = await self._save_attachment(part, vault_id)

            # Enqueue for processing
            if self.background_processor is not None:
                await self.background_processor.enqueue(
                    file_path=file_path,
                    source='email',
                    email_subject=subject,
                    email_sender=sender,
                    vault_id=vault_id,
                )
            else:
                logger.warning("background_processor is None — skipping enqueue for file: %s", file_path)
            processed_attachments += 1
            logger.info(f"Enqueued attachment for processing: {file_path}")

        if processed_attachments == 0:
            logger.info(f"No valid document attachments found in email from {self._sanitize_log_value(sender)}")
        else:
            logger.info(
                f"Processed {processed_attachments} attachment(s) from email from "
                f"{self._sanitize_log_value(sender)}"
            )

    def _sanitize_log_value(self, value: str) -> str:
        """
        Sanitize a value for logging by removing newlines and control characters.

        Prevents log injection attacks where malicious email content could
        inject newlines or control characters into logs.

        Args:
            value: Raw value to sanitize

        Returns:
            Sanitized value with newlines and control characters replaced with spaces
        """
        if not value:
            return ""
        # Replace newlines, carriage returns, tabs and other control characters
        return re.sub(r'[\r\n\t\x00-\x1f\x7f-\x9f]', ' ', str(value))

    def _sanitize_filename(self, filename: str) -> str:
        """
        Sanitize filename to prevent path traversal attacks.

        Uses os.path.basename() to extract just the filename portion,
        removing any directory traversal attempts.

        Args:
            filename: Raw filename from email attachment

        Returns:
            Sanitized filename with path traversal removed
        """
        if not filename:
            return filename
        # Use basename to extract just the filename, removing any path components
        return os.path.basename(filename)

    def _extract_vault_name(self, subject: str) -> Optional[str]:
        """
        Extract vault name from subject tag [VaultName] or #vaultname.

        Args:
            subject: Email subject line

        Returns:
            First matching vault name or None
        """
        # Pattern for [VaultName] - captures content inside brackets
        bracket_pattern = r'\[([^\]]+)\]'
        match = re.search(bracket_pattern, subject)
        if match:
            return match.group(1).strip()

        # Pattern for #vaultname - captures hashtag word
        hashtag_pattern = r'#(\w+)'
        match = re.search(hashtag_pattern, subject)
        if match:
            return match.group(1).strip()

        return None

    def _validate_attachment(self, part) -> tuple[bool, str]:
        """
        Validate attachment by MIME type and file size.

        Checks MIME type against whitelist (imap_allowed_mime_types)
        and file size against max (imap_max_attachment_size).
        Checks actual payload size instead of Content-Length header.

        Args:
            part: Email message part (attachment)

        Returns:
            Tuple of (is_valid, reason) where reason explains why invalid
        """
        # Check MIME type
        content_type = part.get_content_type()
        if content_type not in self.settings.imap_allowed_mime_types:
            return False, f"Disallowed MIME type: {content_type}"

        # Check actual payload size (not Content-Length header which can be spoofed)
        payload = part.get_payload(decode=True)
        if payload:
            size_bytes = len(payload)
            if size_bytes > self.settings.imap_max_attachment_size:
                size_mb = size_bytes / (1024 * 1024)
                max_mb = self.settings.imap_max_attachment_size / (1024 * 1024)
                return False, f"File too large: {size_mb:.2f}MB (max {max_mb:.2f}MB)"

        return True, ""

    async def _save_attachment(self, part, vault_id: int) -> str:
        """
        Save attachment to a temporary file in vault-specific uploads directory.

        Uses tempfile.mkstemp() in vault-specific upload dir with appropriate
        extension based on filename.

        Args:
            part: Email message part (attachment)
            vault_id: Target vault ID for upload directory

        Returns:
            Path to the saved temp file

        Raises:
            Exception: If saving fails
        """
        # Get filename and sanitize to prevent path traversal
        filename = part.get_filename()
        if not filename:
            # Generate a filename with timestamp
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"email_attachment_{timestamp}.dat"
        else:
            # Sanitize filename to prevent path traversal attacks
            filename = self._sanitize_filename(filename)

        # Determine file extension
        content_type = part.get_content_type()
        ext_map = {
            'application/pdf': '.pdf',
            'text/plain': '.txt',
            'text/markdown': '.md',
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
            'text/csv': '.csv',
            'application/json': '.json',
            'application/sql': '.sql',
            'text/x-python': '.py',
            'application/javascript': '.js',
            'text/html': '.html',
            'text/css': '.css',
            'application/xml': '.xml',
            'application/x-yaml': '.yaml',
            'text/x-log': '.log',
        }
        ext = ext_map.get(content_type, '.bin')

        # Use vault-specific upload directory
        provider = UploadPathProvider()
        upload_dir = provider.get_upload_dir(vault_id)

        # Ensure uploads directory exists
        upload_dir.mkdir(parents=True, exist_ok=True)

        # Create temp file
        fd, temp_path = tempfile.mkstemp(
            prefix="email_",
            suffix=ext,
            dir=upload_dir
        )

        try:
            # Write attachment content
            payload = part.get_payload(decode=True)
            if payload:
                os.write(fd, payload)

            logger.debug(f"Saved attachment to: {temp_path}")
            return temp_path
        except (OSError, RuntimeError, ValueError) as e:
            # Clean up temp file on error
            try:
                try:
                    os.close(fd)
                except (OSError, FileNotFoundError):
                    pass
                try:
                    os.unlink(temp_path)
                except (OSError, FileNotFoundError):
                    pass
            finally:
                fd = None  # sentinel — ALWAYS set, even if cleanup raises
            raise Exception(f"Failed to save attachment: {e}")
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except (OSError, FileNotFoundError):
                    pass

    async def _resolve_vault_id(self, vault_name: Optional[str]) -> int:
        """
        Resolve vault ID from vault name (case-insensitive).

        Raises ValueError if vault_name is None or vault is not found.
        """
        if vault_name is None:
            raise ValueError(
                "Email has no vault tag. Configure a [VaultName] subject prefix or reject the email."
            )

        conn = self.pool.get_connection()
        try:
            cursor = conn.execute(
                "SELECT id FROM vaults WHERE LOWER(name) = LOWER(?)",
                (vault_name,)
            )
            row = cursor.fetchone()
            if row:
                vault_id = row["id"]
                logger.debug(f"Resolved vault '{vault_name}' to id={vault_id}")
                return vault_id

            raise ValueError(
                f"Email references vault '{vault_name}' which does not exist in the database. Rejecting email."
            )
        finally:
            self.pool.release_connection(conn)

    def _decode_header_value(self, value: str) -> str:
        """
        Decode email header value (handles encoded words).

        Args:
            value: Raw header value

        Returns:
            Decoded header value as string
        """
        if not value:
            return ""

        decoded_parts = []
        for part, encoding in decode_header(value):
            if isinstance(part, bytes):
                try:
                    decoded_parts.append(part.decode(encoding or 'utf-8', errors='replace'))
                except (LookupError, UnicodeDecodeError):
                    decoded_parts.append(part.decode('utf-8', errors='replace'))
            else:
                decoded_parts.append(str(part))

        return ''.join(decoded_parts)

    def _sanitize_html(self, html: str) -> str:
        """
        Sanitize HTML content using bleach.

        Args:
            html: Raw HTML content

        Returns:
            Sanitized HTML with allowed tags only
        """
        return bleach.clean(
            html,
            tags=ALLOWED_TAGS,
            strip=True,
            strip_comments=True
        )
