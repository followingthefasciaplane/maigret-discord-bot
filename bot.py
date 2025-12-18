#!/usr/bin/env python3

import asyncio
import json
import logging
import os
import re
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import aiosqlite
import discord
import yaml
from discord import app_commands
from discord.ext import commands

import maigret
from maigret.report import (
    generate_report_context,
    save_html_report,
    save_pdf_report,
)
from maigret.sites import MaigretDatabase

# =============================================================================
# constants and configuration
# =============================================================================

VERSION = "2.0.0"
CONFIG_FILE = Path(os.getenv("MAIGRET_BOT_CONFIG", "config.yaml"))
DEFAULT_DATABASE_FILE = "data/bot.db"
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_MAIGRET_DB = "data.json"
DEFAULT_COOKIES_FILE = "cookies.txt"

USERNAME_REGEXP = r"^[a-zA-Z0-9\-_\.]{3,64}$"
EMBED_DESCRIPTION_LIMIT = 4096
EMBED_FIELD_VALUE_LIMIT = 1024
EMBEDS_PER_MESSAGE = 10
MAX_EMBED_FIELDS = 25

# rate limit safe intervals (in seconds)
EDIT_INTERVAL = 2.0
PROGRESS_UPDATE_INTERVAL = 3.0

# hard limits
TOP_SITES_HARD_LIMIT = 1500
TIMEOUT_HARD_LIMIT = 300
MAX_CONNECTIONS_HARD_LIMIT = 200
RETRIES_HARD_LIMIT = 5
MAX_LINKS_HARD_LIMIT = 3000

ALLOWED_MENTIONS = discord.AllowedMentions.none()


# =============================================================================
# logging 
# =============================================================================


DEFAULT_LOGS_DIR = Path("logs")


def setup_logging(level: str = "INFO", file_logging: bool = False) -> None:
    """configure logging for the bot.

    args:
        level: log level for console output
        file_logging: if True, also write logs to daily file in logs/
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    # create formatter
    formatter = logging.Formatter(
        fmt="[%(asctime)s] [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(log_level)

    # root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG if file_logging else log_level)
    root_logger.addHandler(console_handler)

    # file handler - only if enabled
    if file_logging:
        DEFAULT_LOGS_DIR.mkdir(parents=True, exist_ok=True)
        log_file = DEFAULT_LOGS_DIR / f"bot_{datetime.now().strftime('%Y%m%d')}.log"
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.DEBUG)
        root_logger.addHandler(file_handler)

    # suppress noisy loggers
    for noisy_logger in ["discord", "discord.http", "discord.gateway", "aiosqlite", "asyncio"]:
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)


# =============================================================================
# configuration dataclasses
# =============================================================================


@dataclass
class SearchDefaults:
    """default search parameters."""
    top_sites: int = 500
    timeout: int = 30
    max_connections: int = 50
    retries: int = 1
    parsing_enabled: bool = True
    include_similar: bool = False
    id_type: str = "username"


@dataclass
class BotConfig:
    """bot configuration loaded from YAML."""
    discord_token: str = ""
    owner_id: int = 0
    guild_id: Optional[int] = None

    database_file: str = DEFAULT_DATABASE_FILE
    maigret_db_file: str = DEFAULT_MAIGRET_DB
    cookies_file: str = DEFAULT_COOKIES_FILE
    reports_dir: str = str(DEFAULT_REPORTS_DIR)

    log_level: str = "INFO"
    file_logging_enabled: bool = False

    debug_channel_id: Optional[int] = None
    user_log_channel_id: Optional[int] = None
    output_log_channel_id: Optional[int] = None

    search_defaults: SearchDefaults = field(default_factory=SearchDefaults)
    
    @classmethod
    def load(cls, path: Path) -> "BotConfig":
        """load configuration from YAML file."""
        config = cls()
        
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                
                config.discord_token = data.get("discord_token", "") or os.getenv("DISCORD_TOKEN", "")
                config.owner_id = int(data.get("owner_id", 0))
                config.guild_id = int(data["guild_id"]) if data.get("guild_id") else None
                
                config.database_file = data.get("database_file", DEFAULT_DATABASE_FILE)
                config.maigret_db_file = data.get("maigret_db_file", DEFAULT_MAIGRET_DB)
                config.cookies_file = data.get("cookies_file", DEFAULT_COOKIES_FILE)
                config.reports_dir = data.get("reports_dir", str(DEFAULT_REPORTS_DIR))
                
                config.log_level = data.get("log_level", "INFO")
                config.file_logging_enabled = data.get("file_logging_enabled", False)

                config.debug_channel_id = int(data["debug_channel_id"]) if data.get("debug_channel_id") else None
                config.user_log_channel_id = int(data["user_log_channel_id"]) if data.get("user_log_channel_id") else None
                config.output_log_channel_id = int(data["output_log_channel_id"]) if data.get("output_log_channel_id") else None
                
                if "search_defaults" in data:
                    sd = data["search_defaults"]
                    config.search_defaults = SearchDefaults(
                        top_sites=sd.get("top_sites", 500),
                        timeout=sd.get("timeout", 30),
                        max_connections=sd.get("max_connections", 50),
                        retries=sd.get("retries", 1),
                        parsing_enabled=sd.get("parsing_enabled", True),
                        include_similar=sd.get("include_similar", False),
                        id_type=sd.get("id_type", "username"),
                    )
            except Exception as e:
                logging.warning("failed to load config from %s: %s", path, e)
        else:
            # Try environment variables as fallback
            config.discord_token = os.getenv("DISCORD_TOKEN", "")
            owner_id_str = os.getenv("OWNER_ID", "0")
            config.owner_id = int(owner_id_str) if owner_id_str.isdigit() else 0
        
        return config
    
    def save(self, path: Path) -> None:
        """save configuration to YAML file."""
        data = {
            "discord_token": self.discord_token,
            "owner_id": self.owner_id,
            "guild_id": self.guild_id,
            "database_file": self.database_file,
            "maigret_db_file": self.maigret_db_file,
            "cookies_file": self.cookies_file,
            "reports_dir": self.reports_dir,
            "log_level": self.log_level,
            "file_logging_enabled": self.file_logging_enabled,
            "debug_channel_id": self.debug_channel_id,
            "user_log_channel_id": self.user_log_channel_id,
            "output_log_channel_id": self.output_log_channel_id,
            "search_defaults": {
                "top_sites": self.search_defaults.top_sites,
                "timeout": self.search_defaults.timeout,
                "max_connections": self.search_defaults.max_connections,
                "retries": self.search_defaults.retries,
                "parsing_enabled": self.search_defaults.parsing_enabled,
                "include_similar": self.search_defaults.include_similar,
                "id_type": self.search_defaults.id_type,
            },
        }
        
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)


# =============================================================================
# Database Layer
# =============================================================================


class BotDatabase:
    """SQLite database for bot state and permissions."""
    
    def __init__(self, connection: aiosqlite.Connection) -> None:
        self.conn = connection
    
    async def initialize(self) -> None:
        """create database tables if they don't exist."""
        await self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS whitelist (
                user_id INTEGER PRIMARY KEY,
                added_by INTEGER NOT NULL,
                added_at TEXT NOT NULL,
                notes TEXT
            );
            
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            
            CREATE TABLE IF NOT EXISTS search_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                sites_checked INTEGER DEFAULT 0,
                sites_found INTEGER DEFAULT 0,
                duration_seconds REAL DEFAULT 0,
                timestamp TEXT NOT NULL
            );
            
            CREATE INDEX IF NOT EXISTS idx_search_history_user 
                ON search_history(user_id);
            CREATE INDEX IF NOT EXISTS idx_search_history_timestamp 
                ON search_history(timestamp);
        """)
        await self.conn.commit()
    
    # -------------------------------------------------------------------------
    # whitelist operations
    # -------------------------------------------------------------------------
    
    async def add_to_whitelist(
        self,
        user_id: int,
        added_by: int,
        notes: Optional[str] = None,
    ) -> bool:
        """add a user to the whitelist. returns true if added, false if already exists."""
        try:
            await self.conn.execute(
                """
                INSERT INTO whitelist (user_id, added_by, added_at, notes)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, added_by, datetime.now(timezone.utc).isoformat(), notes),
            )
            await self.conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False
    
    async def remove_from_whitelist(self, user_id: int) -> bool:
        """remove a user from the whitelist. returns true if removed."""
        cursor = await self.conn.execute(
            "DELETE FROM whitelist WHERE user_id = ?",
            (user_id,),
        )
        await self.conn.commit()
        return cursor.rowcount > 0
    
    async def is_whitelisted(self, user_id: int) -> bool:
        """check if a user is whitelisted."""
        cursor = await self.conn.execute(
            "SELECT 1 FROM whitelist WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        return row is not None
    
    async def get_whitelist(self) -> List[Dict[str, Any]]:
        """get all whitelisted users."""
        cursor = await self.conn.execute(
            "SELECT user_id, added_by, added_at, notes FROM whitelist ORDER BY added_at DESC"
        )
        rows = await cursor.fetchall()
        return [
            {
                "user_id": row[0],
                "added_by": row[1],
                "added_at": row[2],
                "notes": row[3],
            }
            for row in rows
        ]
    
    # -------------------------------------------------------------------------
    # Settings Operations
    # -------------------------------------------------------------------------
    
    async def get_setting(self, key: str) -> Optional[str]:
        """get a setting value."""
        cursor = await self.conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            (key,),
        )
        row = await cursor.fetchone()
        return row[0] if row else None
    
    async def set_setting(self, key: str, value: str) -> None:
        """set a setting value."""
        await self.conn.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        await self.conn.commit()
    
    async def delete_setting(self, key: str) -> bool:
        """delete a setting. Returns True if deleted."""
        cursor = await self.conn.execute(
            "DELETE FROM settings WHERE key = ?",
            (key,),
        )
        await self.conn.commit()
        return cursor.rowcount > 0
    
    async def get_all_settings(self) -> Dict[str, str]:
        """get all settings."""
        cursor = await self.conn.execute("SELECT key, value FROM settings")
        rows = await cursor.fetchall()
        return {row[0]: row[1] for row in rows}
    
    # -------------------------------------------------------------------------
    # search history
    # -------------------------------------------------------------------------
    
    async def log_search(
        self,
        user_id: int,
        username: str,
        sites_checked: int,
        sites_found: int,
        duration_seconds: float,
    ) -> None:
        """log a completed search."""
        await self.conn.execute(
            """
            INSERT INTO search_history 
                (user_id, username, sites_checked, sites_found, duration_seconds, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                username,
                sites_checked,
                sites_found,
                duration_seconds,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await self.conn.commit()


# =============================================================================
# Permission System
# =============================================================================


class PermissionLevel(Enum):
    """user permission levels."""
    NONE = 0
    MEMBER = 1
    WHITELISTED = 2
    OWNER = 3


async def get_permission_level(
    user_id: int,
    owner_id: int,
    database: BotDatabase,
) -> PermissionLevel:
    """determine a user's permission level."""
    if user_id == owner_id:
        return PermissionLevel.OWNER
    
    if await database.is_whitelisted(user_id):
        return PermissionLevel.WHITELISTED
    
    return PermissionLevel.MEMBER


def require_permission(minimum: PermissionLevel):
    """decorator to require a minimum permission level for commands."""
    async def predicate(interaction: discord.Interaction) -> bool:
        bot: MaigretBot = interaction.client  # type: ignore
        level = await get_permission_level(
            interaction.user.id,
            bot.config.owner_id,
            bot.database,
        )
        
        if level.value < minimum.value:
            level_names = {
                PermissionLevel.WHITELISTED: "whitelisted users",
                PermissionLevel.OWNER: "the bot owner",
            }
            required_name = level_names.get(minimum, "authorized users")
            
            embed = discord.Embed(
                title="permission denied",
                description=f"this command is only available to **{required_name}**.",
                color=discord.Color.red(),
            )
            await interaction.response.send_message(embed=embed)
            return False
        
        return True
    
    return app_commands.check(predicate)


# =============================================================================
# utility functions
# =============================================================================


def clamp(value: int, min_value: int, max_value: int) -> int:
    """clamp an integer value to a range."""
    return max(min_value, min(max_value, value))


def safe_label(text: str, max_length: int = 100) -> str:
    """sanitize text for safe display in embeds."""
    # remove characters that could be used for markdown injection
    cleaned = re.sub(r"[\[\]()@#*_~`|\\]", "", str(text))
    cleaned = cleaned.replace("\n", " ").replace("\r", " ").strip()
    if not cleaned:
        return "unknown"
    return cleaned[:max_length]


def normalize_csv(value: Optional[str]) -> List[str]:
    """parse a comma-separated string into a list."""
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def format_duration(seconds: float) -> str:
    """format duration in a human-readable way."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    remaining = seconds % 60
    return f"{minutes}m {remaining:.0f}s"


def chunk_text(text: str, max_length: int) -> List[str]:
    """split text into chunks that fit within a limit."""
    if len(text) <= max_length:
        return [text]
    
    chunks = []
    lines = text.split("\n")
    current_chunk = ""
    
    for line in lines:
        if len(current_chunk) + len(line) + 1 > max_length:
            if current_chunk:
                chunks.append(current_chunk)
            current_chunk = line[:max_length]
        else:
            current_chunk = f"{current_chunk}\n{line}" if current_chunk else line
    
    if current_chunk:
        chunks.append(current_chunk)
    
    return chunks


def is_claimed(status_obj: Any) -> bool:
    """check if a maigret status indicates the username is claimed."""
    state = getattr(status_obj, "status", None)
    name = getattr(state, "name", None)
    if isinstance(name, str):
        return name.upper() == "CLAIMED"
    return str(state).strip().upper() == "CLAIMED"


# =============================================================================
# Paginator View
# =============================================================================


class PaginatorView(discord.ui.View):
    """a view for paginating through embeds."""
    
    def __init__(
        self,
        embeds: List[discord.Embed],
        *,
        author_id: int,
        timeout: float = 300.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.embeds = embeds
        self.author_id = author_id
        self.current_page = 0
        self.message: Optional[discord.Message] = None
        
        self._update_buttons()
    
    def _update_buttons(self) -> None:
        """update button states based on current page."""
        self.first_button.disabled = self.current_page == 0
        self.prev_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page >= len(self.embeds) - 1
        self.last_button.disabled = self.current_page >= len(self.embeds) - 1
        
        self.page_indicator.label = f"{self.current_page + 1}/{len(self.embeds)}"
    
    def _get_embed(self) -> discord.Embed:
        """get the current page's embed."""
        return self.embeds[self.current_page]
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """only allow the original author to use buttons."""
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "you cannot control this paginator.",
            )
            return False
        return True
    
    async def on_timeout(self) -> None:
        """disable buttons when the view times out."""
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass
    
    @discord.ui.button(label="⏮", style=discord.ButtonStyle.secondary)
    async def first_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.current_page = 0
        self._update_buttons()
        await interaction.response.edit_message(embed=self._get_embed(), view=self)
    
    @discord.ui.button(label="◀", style=discord.ButtonStyle.primary)
    async def prev_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.current_page = max(0, self.current_page - 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self._get_embed(), view=self)
    
    @discord.ui.button(label="1/1", style=discord.ButtonStyle.secondary, disabled=True)
    async def page_indicator(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        pass  # this button is just a page indicator
    
    @discord.ui.button(label="▶", style=discord.ButtonStyle.primary)
    async def next_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.current_page = min(len(self.embeds) - 1, self.current_page + 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self._get_embed(), view=self)
    
    @discord.ui.button(label="⏭", style=discord.ButtonStyle.secondary)
    async def last_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.current_page = len(self.embeds) - 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self._get_embed(), view=self)


# =============================================================================
# search progress view
# =============================================================================


class SearchProgressView(discord.ui.View):
    """view for displaying search progress with a cancel button."""

    def __init__(self, *, author_id: int, timeout: float = 600.0) -> None:
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.cancelled = False
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "only the person who started this search can cancel it.",
            )
            return False
        return True

    async def on_timeout(self) -> None:
        """disable buttons when the view times out."""
        self.disable_all()
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

    @discord.ui.button(label="cancel search", style=discord.ButtonStyle.danger, emoji="✖")
    async def cancel_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.cancelled = True
        button.disabled = True
        button.label = "cancelling..."
        await interaction.response.edit_message(view=self)

    def disable_all(self) -> None:
        """disable all buttons."""
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True


# =============================================================================
# maigret integration
# =============================================================================


_maigret_db: Optional[MaigretDatabase] = None
_maigret_db_lock = asyncio.Lock()


async def get_maigret_db(db_file: str) -> MaigretDatabase:
    """get or load the maigret database."""
    global _maigret_db
    
    if _maigret_db is not None:
        return _maigret_db
    
    async with _maigret_db_lock:
        if _maigret_db is None:
            _maigret_db = await asyncio.to_thread(
                MaigretDatabase().load_from_path, db_file
            )
    
    return _maigret_db


@dataclass
class SearchOptions:
    """options for a maigret search."""
    top_sites: int = 500
    timeout: int = 30
    max_connections: int = 50
    retries: int = 1
    parsing_enabled: bool = True
    include_similar: bool = False
    id_type: str = "username"
    tags: List[str] = field(default_factory=list)
    sites: List[str] = field(default_factory=list)
    
    def validated(self) -> "SearchOptions":
        """return a copy with validated/clamped values."""
        return SearchOptions(
            top_sites=clamp(self.top_sites, 1, TOP_SITES_HARD_LIMIT),
            timeout=clamp(self.timeout, 1, TIMEOUT_HARD_LIMIT),
            max_connections=clamp(self.max_connections, 1, MAX_CONNECTIONS_HARD_LIMIT),
            retries=clamp(self.retries, 0, RETRIES_HARD_LIMIT),
            parsing_enabled=self.parsing_enabled,
            include_similar=self.include_similar,
            id_type=self.id_type or "username",
            tags=list(self.tags),
            sites=list(self.sites),
        )


@dataclass
class SearchResult:
    """results from a maigret search."""
    username: str
    found_accounts: List[Dict[str, str]]  # list of {site, url}
    total_found: int
    total_checked: int
    duration_seconds: float
    errors: int = 0


async def run_maigret_search(
    username: str,
    options: SearchOptions,
    maigret_db: MaigretDatabase,
    cookies_file: Optional[str] = None,
) -> Tuple[Dict[str, Any], int]:
    """
    run a Maigret search.

    returns:
        tuple of (results_dict, errors_count)
    """
    logger = logging.getLogger("maigret.search")

    # build site dictionary
    kwargs: Dict[str, Any] = {
        "top": options.top_sites,
        "disabled": False,
        "id_type": options.id_type,
    }
    if options.tags:
        kwargs["tags"] = list(options.tags)
    if options.sites:
        kwargs["names"] = list(options.sites)

    site_dict = maigret_db.ranked_sites_dict(**kwargs)

    # setup notifier
    query_notify = None
    try:
        from maigret.notify import Notifier
        query_notify = Notifier(result=None, verbose=False)
    except ImportError:
        logger.debug("maigret.notify.Notifier not available")
    except Exception as e:
        logger.warning("failed to initialize Notifier: %s", e)
    
    # build search kwargs
    search_kwargs: Dict[str, Any] = {
        "username": username,
        "site_dict": site_dict,
        "logger": logging.getLogger("maigret.search"),
        "timeout": options.timeout,
        "id_type": options.id_type,
        "is_parsing_enabled": options.parsing_enabled,
        "max_connections": options.max_connections,
        "retries": options.retries,
        "no_progressbar": True,
    }
    
    if query_notify:
        search_kwargs["query_notify"] = query_notify
    
    if cookies_file and Path(cookies_file).is_file():
        search_kwargs["cookies"] = cookies_file
    
    # run search
    results = await maigret.search(**search_kwargs)
    
    # count errors
    errors = sum(
        1 for data in results.values()
        if data.get("status") and hasattr(data["status"], "status")
        and str(getattr(data["status"].status, "name", "")).upper() in ("UNKNOWN", "ILLEGAL")
    )
    
    return results, errors


def extract_found_accounts(
    results: Dict[str, Any],
    *,
    include_similar: bool = False,
) -> List[Dict[str, str]]:
    """extract found accounts from Maigret results."""
    found = []
    
    for site, data in results.items():
        try:
            status = data.get("status")
        except Exception:
            continue
        
        if not status or not is_claimed(status):
            continue
        
        if not include_similar and data.get("is_similar"):
            continue
        
        url = data.get("url_user")
        if not url:
            continue
        
        found.append({
            "site": safe_label(str(site)),
            "url": str(url),
        })
    
    return found


async def generate_html_report(
    username: str,
    id_type: str,
    results: Dict[str, Any],
    reports_dir: Path,
) -> Path:
    """generate an HTML report file."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    
    safe_username = re.sub(r"[^a-zA-Z0-9_.-]+", "_", username).strip("._-") or "report"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{safe_username}_{timestamp}.html"
    report_path = reports_dir / filename
    
    report_context = generate_report_context([(username, id_type, results)])
    
    def _write():
        save_html_report(str(report_path), report_context)
    
    await asyncio.to_thread(_write)
    return report_path


async def generate_txt_results(
    username: str,
    found_accounts: List[Dict[str, str]],
    search_result: SearchResult,
    reports_dir: Path,
) -> Path:
    """generate a formatted TXT file with results."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    
    safe_username = re.sub(r"[^a-zA-Z0-9_.-]+", "_", username).strip("._-") or "report"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{safe_username}_{timestamp}.txt"
    report_path = reports_dir / filename
    
    lines = [
        "=" * 60,
        "MAIGRET SEARCH RESULTS",
        "=" * 60,
        "",
        f"username:       {username}",
        f"date/time:      {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"sites checked:  {search_result.total_checked}",
        f"accounts found: {search_result.total_found}",
        f"duration:       {format_duration(search_result.duration_seconds)}",
        "",
        "-" * 60,
        "FOUND ACCOUNTS",
        "-" * 60,
        "",
    ]
    
    if found_accounts:
        for i, account in enumerate(found_accounts, 1):
            lines.append(f"{i:3}. {account['site']}")
            lines.append(f"     {account['url']}")
            lines.append("")
    else:
        lines.append("no accounts found.")
        lines.append("")
    
    lines.extend([
        "-" * 60,
        "END OF REPORT",
        "-" * 60,
    ])
    
    content = "\n".join(lines)
    
    def _write():
        report_path.write_text(content, encoding="utf-8")
    
    await asyncio.to_thread(_write)
    return report_path


# =============================================================================
# main bot class
# =============================================================================


class MaigretBot(commands.Bot):
    """the main bot."""
    
    def __init__(
        self,
        *,
        config: BotConfig,
        database: BotDatabase,
    ) -> None:
        intents = discord.Intents.all() # ideally change this
        super().__init__(
            command_prefix="!",  # prefix commands disabled, using slash only
            intents=intents,
            allowed_mentions=ALLOWED_MENTIONS,
        )
        
        self.config = config
        self.database = database
        self.logger = logging.getLogger("maigret-bot")
        
        # search state - only one search at a time
        self._search_lock = asyncio.Lock()
        self._current_search: Optional[str] = None  # username being searched
        self._current_search_user: Optional[int] = None  # user who started search
        
        # cached channels
        self._debug_channel: Optional[discord.TextChannel] = None
        self._user_log_channel: Optional[discord.TextChannel] = None
        self._output_log_channel: Optional[discord.TextChannel] = None
    
    @property
    def debug_channel(self) -> Optional[discord.TextChannel]:
        return self._debug_channel
    
    @property
    def user_log_channel(self) -> Optional[discord.TextChannel]:
        return self._user_log_channel
    
    @property
    def output_log_channel(self) -> Optional[discord.TextChannel]:
        return self._output_log_channel
    
    async def setup_hook(self) -> None:
        """setup hook called when bot is starting."""
        # load channel references from config, this is redundant for now
        # await self._load_log_channels()
        
        # add command cogs
        await self.add_cog(SearchCommands(self))
        await self.add_cog(WhitelistCommands(self))
        await self.add_cog(OwnerCommands(self))
        await self.add_cog(HelpCommands(self))
        
        # sync commands
        if self.config.guild_id:
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            self.logger.info("synced commands to guild %s", self.config.guild_id)
        else:
            await self.tree.sync()
            self.logger.info("synced commands globally")
    
    async def _load_log_channels(self) -> None:
        """load log channel references."""
        # wait until bot is ready
        # await self.wait_until_ready() again, it'll deadlock but leave this for later
        
        if self.config.debug_channel_id:
            self._debug_channel = self.get_channel(self.config.debug_channel_id)  # type: ignore
        
        if self.config.user_log_channel_id:
            self._user_log_channel = self.get_channel(self.config.user_log_channel_id)  # type: ignore
        
        if self.config.output_log_channel_id:
            self._output_log_channel = self.get_channel(self.config.output_log_channel_id)  # type: ignore
    
    async def refresh_log_channels(self) -> None:
        """refresh log channel references after config change."""
        if self.config.debug_channel_id:
            self._debug_channel = self.get_channel(self.config.debug_channel_id)  # type: ignore
        else:
            self._debug_channel = None
        
        if self.config.user_log_channel_id:
            self._user_log_channel = self.get_channel(self.config.user_log_channel_id)  # type: ignore
        else:
            self._user_log_channel = None
        
        if self.config.output_log_channel_id:
            self._output_log_channel = self.get_channel(self.config.output_log_channel_id)  # type: ignore
        else:
            self._output_log_channel = None
    
    async def log_debug(self, message: str, *, embed: Optional[discord.Embed] = None) -> None:
        """send a debug log message to the debug channel."""
        if not self._debug_channel:
            return
        
        try:
            if embed:
                await self._debug_channel.send(embed=embed)
            else:
                await self._debug_channel.send(
                    f"```\n{message[:1990]}\n```",
                    allowed_mentions=ALLOWED_MENTIONS,
                )
        except Exception as e:
            self.logger.warning("failed to send debug log: %s", e)
    
    async def log_user_action(
        self,
        *,
        user: Union[discord.User, discord.Member],
        action: str,
        details: str = "",
        embed: Optional[discord.Embed] = None,
    ) -> None:
        """log a user action to the user log channel."""
        if not self._user_log_channel:
            return
        
        try:
            if embed is None:
                embed = discord.Embed(
                    title=f"📝 {action}",
                    description=details or None,
                    color=discord.Color.blue(),
                    timestamp=discord.utils.utcnow(),
                )
                embed.set_author(
                    name=str(user),
                    icon_url=user.display_avatar.url if user.display_avatar else None,
                )
                embed.add_field(name="user ID", value=str(user.id), inline=True)
            
            await self._user_log_channel.send(embed=embed, allowed_mentions=ALLOWED_MENTIONS)
        except Exception as e:
            self.logger.warning("failed to send user log: %s", e)
    
    async def archive_report(self, file_path: Path, *, username: str, user: discord.User) -> None:
        """archive a report file to the output log channel."""
        if not self._output_log_channel:
            return
        
        try:
            embed = discord.Embed(
                title="search report archived",
                color=discord.Color.green(),
                timestamp=discord.utils.utcnow(),
            )
            embed.add_field(name="username searched", value=f"`{username}`", inline=True)
            embed.add_field(name="requested by", value=f"{user.mention}", inline=True)
            embed.add_field(name="file", value=file_path.name, inline=True)
            
            await self._output_log_channel.send(
                embed=embed,
                file=discord.File(file_path),
                allowed_mentions=ALLOWED_MENTIONS,
            )
        except Exception as e:
            self.logger.warning("failed to archive report: %s", e)
    
    def is_search_active(self) -> bool:
        """check if a search is currently in progress."""
        return self._current_search is not None
    
    async def close(self) -> None:
        """clean up when bot is closing."""
        self.logger.info("bot is shutting down...")
        await super().close()


# =============================================================================
# search commands cog
# =============================================================================


class SearchCommands(commands.Cog):
    """commands for searching usernames."""
    
    def __init__(self, bot: MaigretBot) -> None:
        self.bot = bot
    
    @app_commands.command(
        name="search",
        description="search for a username across websites",
    )
    @app_commands.describe(
        username="the username to search for",
        top_sites="number of top sites to check (default: 500)",
        tags="filter by tags (comma-separated, e.g., 'social,dating')",
        sites="specific sites to check (comma-separated)",
        timeout="timeout per site in seconds (default: 30)",
        include_similar="include similar/fuzzy matches",
    )
    @require_permission(PermissionLevel.WHITELISTED)
    async def search(
        self,
        interaction: discord.Interaction,
        username: str,
        top_sites: Optional[int] = None,
        tags: Optional[str] = None,
        sites: Optional[str] = None,
        timeout: Optional[int] = None,
        include_similar: bool = False,
    ) -> None:
        """search for a username across websites."""
        # validate username
        username = username.lstrip("@").strip()
        if not re.fullmatch(USERNAME_REGEXP, username):
            embed = discord.Embed(
                title="invalid username",
                description=(
                    "username must be 3-64 characters and can only contain:\n"
                    "• letters (a-z, A-Z)\n"
                    "• numbers (0-9)\n"
                    "• hyphens, underscores, and periods (- _ .)"
                ),
                color=discord.Color.red(),
            )
            await interaction.response.send_message(embed=embed)
            return

        # try to acquire the search lock (non-blocking check then acquire)
        if self.bot._search_lock.locked():
            embed = discord.Embed(
                title="search in progress",
                description=(
                    f"a search is already running for `{self.bot._current_search}`.\n\n"
                    "please wait for it to complete before starting a new search."
                ),
                color=discord.Color.orange(),
            )
            await interaction.response.send_message(embed=embed)
            return

        async with self.bot._search_lock:
            await self._run_search(
                interaction,
                username,
                top_sites=top_sites,
                tags=tags,
                sites=sites,
                timeout=timeout,
                include_similar=include_similar,
            )
    
    async def _run_search(
        self,
        interaction: discord.Interaction,
        username: str,
        *,
        top_sites: Optional[int],
        tags: Optional[str],
        sites: Optional[str],
        timeout: Optional[int],
        include_similar: bool,
    ) -> None:
        """execute the search with progress updates using a single embed."""
        # build options
        defaults = self.bot.config.search_defaults
        options = SearchOptions(
            top_sites=top_sites or defaults.top_sites,
            timeout=timeout or defaults.timeout,
            max_connections=defaults.max_connections,
            retries=defaults.retries,
            parsing_enabled=defaults.parsing_enabled,
            include_similar=include_similar,
            id_type=defaults.id_type,
            tags=normalize_csv(tags),
            sites=normalize_csv(sites),
        ).validated()

        # mark search as active
        self.bot._current_search = username
        self.bot._current_search_user = interaction.user.id

        # track generated files for cleanup
        html_path: Optional[Path] = None
        txt_path: Optional[Path] = None

        # create progress view
        progress_view = SearchProgressView(author_id=interaction.user.id)

        # create the single embed we'll edit throughout
        embed = discord.Embed(
            title=f"searching for `{username}`",
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(
            name=f"requested by {interaction.user.display_name}",
            icon_url=interaction.user.display_avatar.url if interaction.user.display_avatar else None,
        )
        embed.add_field(
            name="configuration",
            value=(
                f"**sites:** {options.top_sites}\n"
                f"**timeout:** {options.timeout}s\n"
                f"**tags:** {', '.join(options.tags) if options.tags else 'all'}"
            ),
            inline=True,
        )
        embed.add_field(
            name="status",
            value="initializing...",
            inline=True,
        )
        embed.set_footer(text="maigret osint search")

        await interaction.response.send_message(embed=embed, view=progress_view)

        # get message for editing
        message = await interaction.original_response()
        progress_view.message = message

        # log user action
        await self.bot.log_user_action(
            user=interaction.user,
            action="search started",
            details=f"username: `{username}`\ntop sites: {options.top_sites}",
        )

        start_time = time.monotonic()
        last_update = start_time

        try:
            # get maigret database
            maigret_db = await get_maigret_db(self.bot.config.maigret_db_file)

            # update progress
            embed.set_field_at(1, name="status", value="searching...", inline=True)
            await message.edit(embed=embed, view=progress_view)

            # create search task
            search_task = asyncio.create_task(
                run_maigret_search(
                    username,
                    options,
                    maigret_db,
                    cookies_file=self.bot.config.cookies_file,
                )
            )

            # progress update loop
            while not search_task.done():
                await asyncio.sleep(1.0)

                if progress_view.cancelled:
                    search_task.cancel()
                    try:
                        await search_task
                    except asyncio.CancelledError:
                        pass

                    embed.title = f"search cancelled: `{username}`"
                    embed.color = discord.Color.red()
                    embed.set_field_at(1, name="status", value="cancelled by user", inline=True)
                    progress_view.disable_all()
                    await message.edit(embed=embed, view=progress_view)
                    return

                # update progress periodically
                now = time.monotonic()
                if now - last_update >= PROGRESS_UPDATE_INTERVAL:
                    elapsed = now - start_time
                    embed.set_field_at(
                        1,
                        name="status",
                        value=f"searching... {format_duration(elapsed)}",
                        inline=True,
                    )
                    try:
                        await message.edit(embed=embed)
                    except Exception:
                        pass
                    last_update = now

            # get results
            results, errors = await search_task
            duration = time.monotonic() - start_time

            # extract found accounts
            found_accounts = extract_found_accounts(
                results,
                include_similar=options.include_similar,
            )

            # create search result
            search_result = SearchResult(
                username=username,
                found_accounts=found_accounts,
                total_found=len(found_accounts),
                total_checked=len(results),
                duration_seconds=duration,
                errors=errors,
            )

            # log to database
            await self.bot.database.log_search(
                user_id=interaction.user.id,
                username=username,
                sites_checked=search_result.total_checked,
                sites_found=search_result.total_found,
                duration_seconds=duration,
            )

            # generate reports
            reports_dir = Path(self.bot.config.reports_dir)

            html_path = await generate_html_report(
                username,
                options.id_type,
                results,
                reports_dir,
            )

            txt_path = await generate_txt_results(
                username,
                found_accounts,
                search_result,
                reports_dir,
            )

            # build final embed
            progress_view.disable_all()

            if search_result.total_found > 0:
                embed.title = f"results for `{username}`"
                embed.color = discord.Color.green()
                embed.description = f"found **{search_result.total_found}** accounts across **{search_result.total_checked}** sites"
            else:
                embed.title = f"no results for `{username}`"
                embed.color = discord.Color.orange()
                embed.description = f"checked **{search_result.total_checked}** sites, no accounts found"

            # clear old fields and rebuild
            embed.clear_fields()

            # add statistics field
            embed.add_field(
                name="statistics",
                value=(
                    f"**found:** {search_result.total_found}\n"
                    f"**checked:** {search_result.total_checked}\n"
                    f"**errors:** {search_result.errors}\n"
                    f"**duration:** {format_duration(duration)}"
                ),
                inline=True,
            )

            # add top results directly in embed (up to 4 rows for clean display)
            if found_accounts:
                top_results = found_accounts[:4]
                results_text = "\n".join(
                    f"[{acc['site']}]({acc['url']})" for acc in top_results
                )
                if len(found_accounts) > 4:
                    results_text += f"\n*+{len(found_accounts) - 4} more*"

                embed.add_field(
                    name="found accounts",
                    value=results_text,
                    inline=True,
                )

            # add file info field with download instructions
            embed.add_field(
                name="reports",
                value=(
                    "**TXT:** plain text summary\n"
                    "**HTML:** full interactive report\n\n"
                    "*download the HTML file and open it in your browser for the complete report*"
                ),
                inline=True,
            )

            # prepare files
            files = []
            if txt_path and txt_path.exists():
                files.append(discord.File(txt_path, filename=txt_path.name))
            if html_path and html_path.exists():
                files.append(discord.File(html_path, filename=html_path.name))

            # edit the same message with final results and attachments
            await message.edit(embed=embed, view=progress_view, attachments=files)

            # archive report
            if html_path and html_path.exists():
                await self.bot.archive_report(
                    html_path,
                    username=username,
                    user=interaction.user,
                )

            # log completion
            await self.bot.log_user_action(
                user=interaction.user,
                action="search completed",
                details=(
                    f"username: `{username}`\n"
                    f"found: {search_result.total_found}\n"
                    f"checked: {search_result.total_checked}\n"
                    f"duration: {format_duration(duration)}"
                ),
            )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.bot.logger.exception("search failed: %s", e)

            embed.title = f"search failed: `{username}`"
            embed.color = discord.Color.red()
            embed.description = None
            embed.clear_fields()
            embed.add_field(
                name="error",
                value=f"```{str(e)[:500]}```",
                inline=False,
            )
            progress_view.disable_all()
            await message.edit(embed=embed, view=progress_view)

            # log error
            await self.bot.log_debug(
                f"search error for {username}: {e}",
                embed=discord.Embed(
                    title="search error",
                    description=f"```{str(e)[:1000]}```",
                    color=discord.Color.red(),
                ),
            )

        finally:
            # clear search state
            self.bot._current_search = None
            self.bot._current_search_user = None
            # note: report files are kept in reports directory for archival
    
    @app_commands.command(
        name="quicksearch",
        description="quick search with default settings",
    )
    @app_commands.describe(
        username="the username to search for",
    )
    @require_permission(PermissionLevel.WHITELISTED)
    async def quicksearch(
        self,
        interaction: discord.Interaction,
        username: str,
    ) -> None:
        """quick search with default settings - simplified for regular users."""
        await self.search.callback(
            self,
            interaction,
            username=username,
            top_sites=None,
            tags=None,
            sites=None,
            timeout=None,
            include_similar=False,
        )
    
    @app_commands.command(
        name="status",
        description="check if a search is currently running",
    )
    async def status(self, interaction: discord.Interaction) -> None:
        """check current search status."""
        if self.bot.is_search_active():
            embed = discord.Embed(
                title="search in progress",
                description=f"currently searching for: **`{self.bot._current_search}`**",
                color=discord.Color.blue(),
            )
            if self.bot._current_search_user:
                embed.add_field(
                    name="started by",
                    value=f"<@{self.bot._current_search_user}>",
                    inline=True,
                )
        else:
            embed = discord.Embed(
                title="ready",
                description="no search is currently in progress. the bot is ready for a new search.",
                color=discord.Color.green(),
            )
        
        await interaction.response.send_message(embed=embed)


# =============================================================================
# whitelist commands cog
# =============================================================================


class WhitelistCommands(commands.GroupCog, group_name="whitelist"):
    """commands for managing the user whitelist."""
    
    def __init__(self, bot: MaigretBot) -> None:
        self.bot = bot
        super().__init__()
    
    @app_commands.command(name="add", description="add a user to the whitelist")
    @app_commands.describe(
        user="the user to add to the whitelist",
        notes="optional notes about why they were added",
    )
    @require_permission(PermissionLevel.OWNER)
    async def add(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        notes: Optional[str] = None,
    ) -> None:
        """add a user to the whitelist."""
        if user.id == self.bot.config.owner_id:
            embed = discord.Embed(
                title="already authorized",
                description="the bot owner automatically has all permissions.",
                color=discord.Color.blue(),
            )
            await interaction.response.send_message(embed=embed)
            return
        
        added = await self.bot.database.add_to_whitelist(
            user_id=user.id,
            added_by=interaction.user.id,
            notes=notes,
        )
        
        if added:
            embed = discord.Embed(
                title="user whitelisted",
                description=f"{user.mention} has been added to the whitelist.",
                color=discord.Color.green(),
            )
            if notes:
                embed.add_field(name="notes", value=notes, inline=False)
            
            await self.bot.log_user_action(
                user=interaction.user,
                action="whitelist add",
                details=f"added: {user} ({user.id})\nnotes: {notes or 'None'}",
            )
        else:
            embed = discord.Embed(
                title="already whitelisted",
                description=f"{user.mention} is already on the whitelist.",
                color=discord.Color.blue(),
            )
        
        await interaction.response.send_message(embed=embed)
    
    @app_commands.command(name="remove", description="remove a user from the whitelist")
    @app_commands.describe(user="the user to remove from the whitelist")
    @require_permission(PermissionLevel.OWNER)
    async def remove(
        self,
        interaction: discord.Interaction,
        user: discord.User,
    ) -> None:
        """remove a user from the whitelist."""
        removed = await self.bot.database.remove_from_whitelist(user.id)
        
        if removed:
            embed = discord.Embed(
                title="user removed",
                description=f"{user.mention} has been removed from the whitelist.",
                color=discord.Color.green(),
            )
            
            await self.bot.log_user_action(
                user=interaction.user,
                action="whitelist remove",
                details=f"removed: {user} ({user.id})",
            )
        else:
            embed = discord.Embed(
                title="not found",
                description=f"{user.mention} was not on the whitelist.",
                color=discord.Color.orange(),
            )
        
        await interaction.response.send_message(embed=embed)
    
    @app_commands.command(name="view", description="view all whitelisted users")
    @require_permission(PermissionLevel.OWNER)
    async def view(self, interaction: discord.Interaction) -> None:
        """view all whitelisted users with pagination."""
        whitelist = await self.bot.database.get_whitelist()
        
        if not whitelist:
            embed = discord.Embed(
                title="whitelist",
                description="no users are currently whitelisted.",
                color=discord.Color.blue(),
            )
            await interaction.response.send_message(embed=embed)
            return
        
        # create paginated embeds
        items_per_page = 10
        pages = []
        
        for i in range(0, len(whitelist), items_per_page):
            chunk = whitelist[i:i + items_per_page]
            
            embed = discord.Embed(
                title="whitelisted users",
                description=f"total: {len(whitelist)} users",
                color=discord.Color.blue(),
            )
            
            for entry in chunk:
                user_id = entry["user_id"]
                added_at = entry["added_at"][:10]  # just the date
                notes = entry["notes"] or "no notes"

                embed.add_field(
                    name=f"user {user_id}",
                    value=f"<@{user_id}>\nadded: {added_at}\n{notes[:100]}",
                    inline=True,
                )
            
            pages.append(embed)
        
        if len(pages) == 1:
            await interaction.response.send_message(embed=pages[0])
        else:
            view = PaginatorView(pages, author_id=interaction.user.id)
            await interaction.response.send_message(embed=pages[0], view=view)
            view.message = await interaction.original_response()


# =============================================================================
# owner commands cog
# =============================================================================


class OwnerCommands(commands.Cog):
    """owner-only configuration commands."""
    
    def __init__(self, bot: MaigretBot) -> None:
        self.bot = bot
    
    @app_commands.command(name="debuglog", description="set the debug log channel")
    @app_commands.describe(channel="the channel for debug logs (leave empty to disable)")
    @require_permission(PermissionLevel.OWNER)
    async def debuglog(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        """set the debug log channel."""
        if channel:
            self.bot.config.debug_channel_id = channel.id
            self.bot._debug_channel = channel
            
            embed = discord.Embed(
                title="debug log channel set",
                description=f"debug logs will be sent to {channel.mention}",
                color=discord.Color.green(),
            )
        else:
            self.bot.config.debug_channel_id = None
            self.bot._debug_channel = None
            
            embed = discord.Embed(
                title="debug log disabled",
                description="debug logging has been disabled.",
                color=discord.Color.green(),
            )
        
        self.bot.config.save(CONFIG_FILE)
        await interaction.response.send_message(embed=embed)
    
    @app_commands.command(name="userlog", description="set the user action log channel")
    @app_commands.describe(channel="the channel for user logs (leave empty to disable)")
    @require_permission(PermissionLevel.OWNER)
    async def userlog(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        """set the user action log channel."""
        if channel:
            self.bot.config.user_log_channel_id = channel.id
            self.bot._user_log_channel = channel
            
            embed = discord.Embed(
                title="user log channel set",
                description=f"user actions will be logged to {channel.mention}",
                color=discord.Color.green(),
            )
        else:
            self.bot.config.user_log_channel_id = None
            self.bot._user_log_channel = None
            
            embed = discord.Embed(
                title="user log disabled",
                description="user action logging has been disabled.",
                color=discord.Color.green(),
            )
        
        self.bot.config.save(CONFIG_FILE)
        await interaction.response.send_message(embed=embed)
    
    @app_commands.command(name="outputlog", description="set the report archive channel")
    @app_commands.describe(channel="the channel for archived reports (leave empty to disable)")
    @require_permission(PermissionLevel.OWNER)
    async def outputlog(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        """set the report archive channel."""
        if channel:
            self.bot.config.output_log_channel_id = channel.id
            self.bot._output_log_channel = channel
            
            embed = discord.Embed(
                title="output log channel set",
                description=f"reports will be archived to {channel.mention}",
                color=discord.Color.green(),
            )
        else:
            self.bot.config.output_log_channel_id = None
            self.bot._output_log_channel = None
            
            embed = discord.Embed(
                title="output log disabled",
                description="report archiving has been disabled.",
                color=discord.Color.green(),
            )
        
        self.bot.config.save(CONFIG_FILE)
        await interaction.response.send_message(embed=embed)
    
    @app_commands.command(name="settings", description="view current bot settings")
    @require_permission(PermissionLevel.OWNER)
    async def settings(self, interaction: discord.Interaction) -> None:
        """view current bot settings."""
        config = self.bot.config
        
        embed = discord.Embed(
            title="bot settings",
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow(),
        )
        
        # general settings
        general = (
            f"**owner ID:** {config.owner_id}\n"
            f"**guild ID:** {config.guild_id or 'Global'}\n"
            f"**log level:** {config.log_level}"
        )
        embed.add_field(name="general", value=general, inline=False)
        
        # channels
        debug_ch = f"<#{config.debug_channel_id}>" if config.debug_channel_id else "Not set"
        user_ch = f"<#{config.user_log_channel_id}>" if config.user_log_channel_id else "Not set"
        output_ch = f"<#{config.output_log_channel_id}>" if config.output_log_channel_id else "Not set"
        
        channels = (
            f"**debug log:** {debug_ch}\n"
            f"**user log:** {user_ch}\n"
            f"**output log:** {output_ch}"
        )
        embed.add_field(name="log channels", value=channels, inline=False)
        
        # search defaults
        sd = config.search_defaults
        defaults = (
            f"**top sites:** {sd.top_sites}\n"
            f"**timeout:** {sd.timeout}s\n"
            f"**max connections:** {sd.max_connections}\n"
            f"**retries:** {sd.retries}\n"
            f"**parsing:** {'Enabled' if sd.parsing_enabled else 'Disabled'}\n"
            f"**include similar:** {'Yes' if sd.include_similar else 'No'}"
        )
        embed.add_field(name="search defaults", value=defaults, inline=False)
        
        # files
        files = (
            f"**database:** `{config.database_file}`\n"
            f"**maigret DB:** `{config.maigret_db_file}`\n"
            f"**reports dir:** `{config.reports_dir}`"
        )
        embed.add_field(name="files", value=files, inline=False)
        
        await interaction.response.send_message(embed=embed)
    
    @app_commands.command(name="setdefault", description="update default search settings")
    @app_commands.describe(
        top_sites="default number of top sites to check",
        timeout="default timeout per site in seconds",
        max_connections="default max concurrent connections",
        retries="default number of retries",
        parsing_enabled="enable parsing by default",
        include_similar="include similar matches by default",
    )
    @require_permission(PermissionLevel.OWNER)
    async def setdefault(
        self,
        interaction: discord.Interaction,
        top_sites: Optional[int] = None,
        timeout: Optional[int] = None,
        max_connections: Optional[int] = None,
        retries: Optional[int] = None,
        parsing_enabled: Optional[bool] = None,
        include_similar: Optional[bool] = None,
    ) -> None:
        """update default search settings."""
        sd = self.bot.config.search_defaults
        changes = []
        
        if top_sites is not None:
            sd.top_sites = clamp(top_sites, 1, TOP_SITES_HARD_LIMIT)
            changes.append(f"top sites: {sd.top_sites}")
        
        if timeout is not None:
            sd.timeout = clamp(timeout, 1, TIMEOUT_HARD_LIMIT)
            changes.append(f"timeout: {sd.timeout}s")
        
        if max_connections is not None:
            sd.max_connections = clamp(max_connections, 1, MAX_CONNECTIONS_HARD_LIMIT)
            changes.append(f"max connections: {sd.max_connections}")
        
        if retries is not None:
            sd.retries = clamp(retries, 0, RETRIES_HARD_LIMIT)
            changes.append(f"retries: {sd.retries}")
        
        if parsing_enabled is not None:
            sd.parsing_enabled = parsing_enabled
            changes.append(f"parsing: {'Enabled' if parsing_enabled else 'Disabled'}")
        
        if include_similar is not None:
            sd.include_similar = include_similar
            changes.append(f"include similar: {'Yes' if include_similar else 'No'}")
        
        if changes:
            self.bot.config.save(CONFIG_FILE)
            
            embed = discord.Embed(
                title="defaults updated",
                description="\n".join(f"• {c}" for c in changes),
                color=discord.Color.green(),
            )
        else:
            embed = discord.Embed(
                title="no changes",
                description="no settings were modified.",
                color=discord.Color.blue(),
            )
        
        await interaction.response.send_message(embed=embed)
    
    @app_commands.command(name="reload", description="reload the Maigret database")
    @require_permission(PermissionLevel.OWNER)
    async def reload(self, interaction: discord.Interaction) -> None:
        """reload the maigret sites database."""
        global _maigret_db

        # prevent reload during active search
        if self.bot.is_search_active():
            embed = discord.Embed(
                title="reload blocked",
                description="cannot reload database while a search is in progress. please wait for the search to complete.",
                color=discord.Color.orange(),
            )
            await interaction.response.send_message(embed=embed)
            return

        await interaction.response.defer()

        try:
            async with _maigret_db_lock:
                _maigret_db = await asyncio.to_thread(
                    MaigretDatabase().load_from_path,
                    self.bot.config.maigret_db_file,
                )

            embed = discord.Embed(
                title="database reloaded",
                description="the maigret sites database has been reloaded.",
                color=discord.Color.green(),
            )
        except Exception as e:
            embed = discord.Embed(
                title="reload failed",
                description=f"```{str(e)[:500]}```",
                color=discord.Color.red(),
            )

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="cleanuplogs", description="delete old log files")
    @app_commands.describe(days="delete logs older than this many days (default: 7)")
    @require_permission(PermissionLevel.OWNER)
    async def cleanuplogs(
        self,
        interaction: discord.Interaction,
        days: int = 7,
    ) -> None:
        """delete old log files from the logs directory."""
        if days < 1:
            days = 1

        await interaction.response.defer()

        deleted_count = 0
        deleted_size = 0
        errors = []

        if DEFAULT_LOGS_DIR.exists():
            cutoff = datetime.now() - timedelta(days=days)

            for log_file in DEFAULT_LOGS_DIR.glob("*.log"):
                try:
                    # check file modification time
                    mtime = datetime.fromtimestamp(log_file.stat().st_mtime)
                    if mtime < cutoff:
                        size = log_file.stat().st_size
                        log_file.unlink()
                        deleted_count += 1
                        deleted_size += size
                except Exception as e:
                    errors.append(f"{log_file.name}: {e}")

        if deleted_count > 0:
            size_str = f"{deleted_size / 1024:.1f} KB" if deleted_size < 1024 * 1024 else f"{deleted_size / 1024 / 1024:.1f} MB"
            embed = discord.Embed(
                title="logs cleaned up",
                description=f"deleted **{deleted_count}** log files ({size_str}) older than {days} days.",
                color=discord.Color.green(),
            )
        else:
            embed = discord.Embed(
                title="no logs to clean",
                description=f"no log files older than {days} days found.",
                color=discord.Color.blue(),
            )

        if errors:
            embed.add_field(
                name="errors",
                value="\n".join(errors[:5]) + (f"\n...and {len(errors) - 5} more" if len(errors) > 5 else ""),
                inline=False,
            )

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="togglefilelogs", description="enable or disable file logging")
    @require_permission(PermissionLevel.OWNER)
    async def togglefilelogs(
        self,
        interaction: discord.Interaction,
    ) -> None:
        """toggle file logging on or off. requires bot restart to take effect."""
        self.bot.config.file_logging_enabled = not self.bot.config.file_logging_enabled
        self.bot.config.save(CONFIG_FILE)

        status = "enabled" if self.bot.config.file_logging_enabled else "disabled"
        embed = discord.Embed(
            title=f"file logging {status}",
            description=f"file logging has been **{status}**.\n\n*restart the bot for this change to take effect.*",
            color=discord.Color.green() if self.bot.config.file_logging_enabled else discord.Color.orange(),
        )

        await interaction.response.send_message(embed=embed)


# =============================================================================
# help commands cog
# =============================================================================


class HelpCommands(commands.Cog):
    """help and information commands - available to everyone."""
    
    def __init__(self, bot: MaigretBot) -> None:
        self.bot = bot
    
    @app_commands.command(name="help", description="show help information")
    async def help(self, interaction: discord.Interaction) -> None:
        """show help information with pagination."""
        # get user's permission level
        level = await get_permission_level(
            interaction.user.id,
            self.bot.config.owner_id,
            self.bot.database,
        )
        
        pages = []
        
        # page 1: introduction
        intro = discord.Embed(
            title="maigret bot - help",
            description=(
                "**what is maigret?**\n"
                "maigret is an OSINT tool that searches "
                "for usernames across thousands of websites to find where a person "
                "has registered accounts.\n\n"
                "**how it works:**\n"
                "1. you provide a username\n"
                "2. the bot checks if that username exists on various sites\n"
                "3. results are provided as downloadable reports\n\n"
                "**important notes:**\n"
                "• results show where a username *exists*, not necessarily that it's the same person\n"
                "• some sites may block automated checks\n"
                "• use this tool responsibly and ethically"
            ),
            color=discord.Color.blue(),
        )
        intro.set_footer(text="page 1 • use the buttons to navigate")
        pages.append(intro)
        
        # page 2: commands based on permission level
        commands_embed = discord.Embed(
            title="available commands",
            color=discord.Color.blue(),
        )
        
        # everyone can see help
        commands_embed.add_field(
            name="general commands",
            value=(
                "`/help` - show this help menu\n"
                "`/status` - check if a search is running\n"
                "`/about` - bot version and statistics"
            ),
            inline=False,
        )
        
        if level.value >= PermissionLevel.WHITELISTED.value:
            commands_embed.add_field(
                name="search commands (whitelisted users)",
                value=(
                    "`/quicksearch <username>` - simple search with default settings\n"
                    "`/search <username> [options]` - full search with options:\n"
                    "  • `top_sites` - number of sites to check\n"
                    "  • `tags` - filter by category (e.g., 'social,gaming')\n"
                    "  • `sites` - check specific sites only\n"
                    "  • `timeout` - per-site timeout\n"
                    "  • `include_similar` - include fuzzy matches"
                ),
                inline=False,
            )
        
        if level == PermissionLevel.OWNER:
            commands_embed.add_field(
                name="owner commands",
                value=(
                    "`/whitelist add/remove/view` - manage whitelist\n"
                    "`/settings` - view bot settings\n"
                    "`/setdefault` - update default settings\n"
                    "`/debuglog` - set debug log channel\n"
                    "`/userlog` - set user action log channel\n"
                    "`/outputlog` - set report archive channel\n"
                    "`/reload` - reload maigret database\n"
                    "`/togglefilelogs` - enable/disable file logging\n"
                    "`/cleanuplogs [days]` - delete old log files"
                ),
                inline=False,
            )
        
        commands_embed.set_footer(text="page 2 • use the buttons to navigate")
        pages.append(commands_embed)
        
        # page 3: tips and usage
        tips = discord.Embed(
            title="tips & best Practices",
            description=(
                "**for best results:**\n"
                "• use usernames without @ symbol\n"
                "• start with fewer `top_sites` for faster results\n"
                "• use `tags` to focus on specific site categories\n\n"
                "**understanding Results:**\n"
                "• **TXT file** - quick text summary of found accounts\n"
                "• **HTML file** - detailed report you can open in a browser\n\n"
                "**ethical usage:**\n"
                "• only search for information you have a legitimate reason to find\n"
                "• respect privacy and don't use for harassment\n"
                "• results are leads for further investigation, not proof\n\n"
                "**rate limits:**\n"
                "• only one search can run at a time\n"
                "• wait for the current search to complete before starting another"
            ),
            color=discord.Color.blue(),
        )
        tips.set_footer(text="page 3 • use the buttons to navigate")
        pages.append(tips)
        
        # page 4: permission info
        perms = discord.Embed(
            title="permission levels",
            description=(
                f"**your level:** {level.name}\n\n"
                "**permission hierarchy:**\n\n"
                "**member** (default)\n"
                "can only use `/help`, `/about` and `/status`\n\n"
                "**whitelisted**\n"
                "can perform username searches\n\n"
                "**owner**\n"
                "full bot control and configuration"
            ),
            color=discord.Color.blue(),
        )
        
        if level == PermissionLevel.MEMBER:
            perms.add_field(
                name="need access?",
                value=f"contact the bot owner (<@{self.bot.config.owner_id}>) to be added to the whitelist.",
                inline=False,
            )
        
        perms.set_footer(text="page 4 • use the buttons to navigate")
        pages.append(perms)
        
        # send with pagination
        view = PaginatorView(pages, author_id=interaction.user.id)
        await interaction.response.send_message(embed=pages[0], view=view)
        view.message = await interaction.original_response()
    
    @app_commands.command(name="about", description="about this bot")
    async def about(self, interaction: discord.Interaction) -> None:
        """show information about the bot."""
        embed = discord.Embed(
            title="about maigret bot",
            description=(
                f"**version:** {VERSION}\n\n"
                "a discord bot interface for [maigret](https://github.com/soxoj/maigret), "
                "an OSINT tool for finding user accounts across many websites.\n\n"
                "**features:**\n"
                "• username search across hundreds of sites\n"
                "• HTML and TXT report generation\n"
                "• permission-based access control\n"
                "• search progress tracking\n"
                "• comprehensive logging"
            ),
            color=discord.Color.blue(),
        )
        
        embed.add_field(
            name="statistics",
            value=(
                f"guilds: {len(self.bot.guilds)}\n"
                f"latency: {self.bot.latency * 1000:.0f}ms"
            ),
            inline=True,
        )
        
        await interaction.response.send_message(embed=embed)


# =============================================================================
# main entry point
# =============================================================================


async def run_bot() -> None:
    """main entry point for running the bot."""
    # load configuration
    config = BotConfig.load(CONFIG_FILE)
    
    if not config.discord_token:
        raise SystemExit("missing discord_token in config file or DISCORD_TOKEN env var.")
    
    if not config.owner_id:
        raise SystemExit("missing owner_id in config file. please set the bot owner's Discord user ID.")
    
    # setup logging
    setup_logging(config.log_level, file_logging=config.file_logging_enabled)
    log = logging.getLogger("maigret-bot")
    log.info("starting maigret discord bot v%s", VERSION)
    log.info("owner ID: %s", config.owner_id)
    
    # initialize database
    db_path = Path(config.database_file)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    
    async with aiosqlite.connect(str(db_path)) as db_conn:
        bot_db = BotDatabase(db_conn)
        await bot_db.initialize()
        
        # create bot instance
        bot = MaigretBot(config=config, database=bot_db)
        
        @bot.event
        async def on_ready() -> None:
            log.info("=" * 50)
            log.info("bot is ready!")
            log.info("logged in as: %s (ID: %s)", bot.user, bot.user.id if bot.user else "unknown")
            log.info("owner ID: %s", config.owner_id)
            log.info("guilds: %d", len(bot.guilds))
            log.info("=" * 50)
            
            # refresh log channels
            await bot.refresh_log_channels()
            
            # log to debug channel if configured
            if bot.debug_channel:
                try:
                    embed = discord.Embed(
                        title="bot started",
                        description=f"maigret bot v{VERSION} is now online.",
                        color=discord.Color.green(),
                        timestamp=discord.utils.utcnow(),
                    )
                    embed.add_field(name="bot user", value=str(bot.user), inline=True)
                    embed.add_field(name="guilds", value=str(len(bot.guilds)), inline=True)
                    embed.add_field(name="owner", value=f"<@{config.owner_id}>", inline=True)
                    await bot.debug_channel.send(embed=embed)
                except Exception as e:
                    log.warning("failed to send startup message to debug channel: %s", e)
        
        @bot.event
        async def on_guild_join(guild: discord.Guild) -> None:
            log.info("joined guild: %s (ID: %s)", guild.name, guild.id)
            if bot.debug_channel:
                try:
                    embed = discord.Embed(
                        title="joined guild",
                        description=f"**{guild.name}**",
                        color=discord.Color.blue(),
                        timestamp=discord.utils.utcnow(),
                    )
                    embed.add_field(name="guild ID", value=str(guild.id), inline=True)
                    embed.add_field(name="members", value=str(guild.member_count), inline=True)
                    await bot.debug_channel.send(embed=embed)
                except Exception:
                    pass
        
        @bot.event
        async def on_guild_remove(guild: discord.Guild) -> None:
            log.info("left guild: %s (ID: %s)", guild.name, guild.id)
            if bot.debug_channel:
                try:
                    embed = discord.Embed(
                        title="left guild",
                        description=f"**{guild.name}**",
                        color=discord.Color.orange(),
                        timestamp=discord.utils.utcnow(),
                    )
                    embed.add_field(name="guild ID", value=str(guild.id), inline=True)
                    await bot.debug_channel.send(embed=embed)
                except Exception:
                    pass
        
        # run the bot
        try:
            async with bot:
                await bot.start(config.discord_token)
        except asyncio.CancelledError:
            log.info("bot shutdown requested.")
        except KeyboardInterrupt:
            log.info("keyboard interrupt received.")
        except Exception as e:
            log.exception("bot crashed with error: %s", e)
            raise
        finally:
            log.info("bot has shut down.")


def main() -> None:
    """synchronous entry point."""
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        print("\nshutdown complete.")
    except SystemExit as e:
        print(f"error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()