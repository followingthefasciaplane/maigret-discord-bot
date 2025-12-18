# maigretcord

a discord bot interface for [maigret](https://github.com/soxoj/maigret). still kind of rough, needs a lot of polish and testing.  

- requires **python 3.10+**
- still heavily beta may have bugs

## features

- **rich embeds** - interactive pagination
- **maigret integration** - maigret lookup is supported for the most part (except for PDF reports)
- **permission system** - three-tier access control (bot host, whitelisted, member)
- **whitelist management** - easy user auth via discord commands
- **channel logging** - separate channels for debug, user actions, and report archiving
- **single search queue** - consistent state management
- **configurable** - customize most things
- **sqlite storage** - persistent storage for various things

## installation

**1. clone this repo**

```bash
git clone https://github.com/followingthefasciaplane/maigret-discord-bot.git
cd maigret-discord-bot
# or alternatively download and extract this repo
```


**2. open terminal and install dependencies**

- setup venv  
  
```bash
python -m venv venv

# linux/mac:
source venv/bin/activate

# windows:
venv\Scripts\activate
```  
  
- install requirements

```bash
pip install -r requirements.txt
```

**3. download maigret data**

```bash
wget https://raw.githubusercontent.com/soxoj/maigret/main/maigret/resources/data.json 
# or optionally use the one in this repo
```

**4. create a discord app and get your token**

- go to [discord dev portal](https://discord.com/developers)
- create a new app
- navigate to the `bot` panel
- enable all 3 privelleged gateway intents (this is bad practice but makes testing easier)
  - if you want to do intents correctly instead of all, go and change [this line](https://github.com/followingthefasciaplane/maigret-discord-bot/blob/master/bot.py#L923) in bot.py aswell. 
- hit `reset token` and copy your bot token into your env or `config.yaml` (it supports it, but your token has zero reason not to be env)
- go to the `installation` panel next and tick `guild install`
- for your guild install scopes you should have `applications.commands` and `bot`
- for your permissions (beneath your scopes) you should have `administrator` (unless you know why you shouldnt have administrator)
- save and copy the link above afterward, then authorize your new application to join your server

**5. setup your config.yaml**

- edit `config.yaml` with your settings.

#### 6. run the bot

```bash
python bot.py
```

### environment variables

you can also use environment variables (useful for docker):

| variable | description | default |
|----------|-------------|---------|
| `DISCORD_TOKEN` | bot token (fallback if not in config) | - |
| `OWNER_ID` | owner user ID (fallback if not in config) | - |
| `MAIGRET_BOT_CONFIG` | path to config file | `config.yaml` |  
  
## command reference

### general commands

these commands are available to everyone:

| command | description |
|---------|-------------|
| `/help` | display comprehensive help with pagination |
| `/status` | check if a search is currently in progress |
| `/about` | show bot version and statistics |

### search commands

these commands require **whitelisted** permission:

| command | description |
|---------|-------------|
| `/quicksearch <username>` | quick search with default settings |
| `/search <username> [options]` | full search with customizable options |

#### `/search` Options

| option | type | description |
|--------|------|-------------|
| `username` | string | **required.** the username to search for |
| `top_sites` | integer | number of top sites to check (1-1500, maigret supports more, raise limit [here](https://github.com/followingthefasciaplane/maigret-discord-bot/blob/master/bot.py#L54)) |
| `tags` | string | comma-separated tags to filter sites (e.g., "social,gaming") |
| `sites` | string | comma-separated specific sites (e.g., "GitHub,Twitter") |
| `timeout` | integer | per-site timeout in seconds (1-300) |
| `include_similar` | boolean | include fuzzy/similar matches (TODO!!) |

### whitelist commands

these commands require **Owner** permission:

| command | description |
|---------|-------------|
| `/whitelist add <user> [notes]` | add a user to the whitelist |
| `/whitelist remove <user>` | remove a user from the whitelist |
| `/whitelist view` | view all whitelisted users with pagination |

### owner commands

these commands require **Owner** permission:

| command | description |
|---------|-------------|
| `/settings` | view all current bot settings |
| `/setdefault [options]` | update default search settings |
| `/debuglog [channel]` | set or clear the debug log channel |
| `/userlog [channel]` | set or clear the user action log channel |
| `/outputlog [channel]` | set or clear the report archive channel |
| `/reload` | reload the maigret sites database |
| `/togglefilelogs` | enable/disable file logging (requires restart) |
| `/cleanuplogs [days]` | delete log files older than specified days (default: 7) |

#### `/setdefault` Options

| option | type | description |
|--------|------|-------------|
| `top_sites` | integer | default number of sites to check |
| `timeout` | integer | default per-site timeout |
| `max_connections` | integer | default concurrent connections |
| `retries` | integer | default retry count |
| `parsing_enabled` | boolean | enable profile parsing by default |
| `include_similar` | boolean | include similar matches by default |

## log channels

the bot supports three separate log channels for different purposes:

### debug log (`/debuglog`)

receives:
- bot startup/shutdown messages
- error details and stack traces
- guild join/leave notifications
- technical debugging information

**recommended:** private channel, owner-only access

### user log (`/userlog`)

receives:
- search requests (who searched what)
- search completions with statistics
- whitelist changes
- user actions

**recommended:** private channel, mod/admin access

### output log (`/outputlog`)

receives:
- archived copies of all generated reports
- report metadata (who requested, when)

**recommended:** private channel, used for record-keeping

### setting up log channels

```
/debuglog channel:#bot-debug
/userlog channel:#user-actions
/outputlog channel:#report-archive
```

to disable a log channel:
```
/debuglog
```
(run without specifying a channel)

## results

### output files

every search generates two files and uploads them both to the channel the command was issued in:

#### TXT file
```
============================================================
MAIGRET SEARCH RESULTS
============================================================

Username:       johndoe
Date/Time:      2024-01-15 14:30:22 UTC
Sites Checked:  500
Accounts Found: 23
Duration:       2m 15s

------------------------------------------------------------
FOUND ACCOUNTS
------------------------------------------------------------

  1. GitHub
     https://github.com/johndoe

  2. Twitter
     https://twitter.com/johndoe

  3. Reddit
     https://reddit.com/user/johndoe

... (continues)
```

#### HTML file

a detailed, interactive report that includes:
- visual formatting
- clickable links
- profile information (if parsing enabled)
- site categories and tags
- additional metadata

## acknowledgments

- [Maigret](https://github.com/soxoj/maigret) - The underlying OSINT tool
- [discord.py](https://github.com/Rapptz/discord.py) - Discord API wrapper
- [maigret-tg-bot](https://github.com/soxoj/maigret-tg-bot) - Maigret Telegram Bot [@soxoj](https://github.com/soxoj)
- [maigret-tg-bot/dev](https://github.com/soxoj/maigret-tg-bot/pull/4) - Updated fork [@rly0nheart](https://github.com/rly0nheart)

