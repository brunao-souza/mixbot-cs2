# MixBot — CS2 Mix Discord Bot

A complete Python bot for managing **mix** (5v5) Counter-Strike 2 matches in Discord communities. Handles player queues, ELO ranking, automatic match creation via RCON on CS2 servers with the **MatchZy** plugin, Steam and FACEIT integration, smurf detection, tournaments, punishment system, VIP via Stripe, and much more.

---

## ✨ Features

### 🎮 Mix / Queue System
- **Smart Queue** — players join the "Next" voice channel and are automatically moved when there are 10 players
- **Accept / Decline mix** — calls the 10 players with buttons and timeout
- **Captain voting** — each player votes for who will be captain
- **Team pick** — captains alternate picking players
- **Map veto** — captains alternate bans until one map remains
- **Automatic match creation** on the CS2 server via RCON + MatchZy
- **Post-match movement**: winners move up, losers move down, next in queue enters

### 📊 Ranking and Stats
- **ELO system** with calculation based on result + individual performance (ADR)
- **Complete player profile** (`/profile`) with kills, deaths, assists, ADR, win streak, total matches
- **Overall ranking** (`/ranking`) — Top community players
- **Match history** (`/history`)
- **Featured MVP** in the match summary (highest damage)

### 🔌 Integrations
- **Steam** — links Discord account to SteamID64, validation, Steam API data fetching
- **FACEIT** — optional profile linking integration
- **CS2 ↔ Discord Chat Bridge** — CS2 chat messages appear in Discord and vice versa
- **CS2 server monitor** — tracks server online/offline status

### 🛡️ Moderation and Community
- **Smurf detection** — suspicious account analysis
- **Report system** — tickets opened by players
- **Automatic punishments** — timeout/ban for declines, abandonment, behavior
- **Welcome messages** for new members
- **Fixed panels** — registration, reports, punishments

### 💳 VIP and Monetization
- **VIP via Stripe** — paid plans with in-server benefits
- **Payments processed by the bot itself** with Stripe integration

### 🏆 Tournaments
- **Tournament system** — brackets, scheduled matches, Discord management

---

## 📋 Prerequisites

Before you start, you will need to have/configure:

- **Python 3.10 or higher** installed on your system
- **MySQL 8** (local or remote) — the bot uses `aiomysql` with connection pooling
- **A CS2 server** running the [**MatchZy**](https://github.com/shobhitp/MatchZy) plugin with RCON enabled (this is a **mandatory prerequisite** — this tutorial does not cover CS2 server installation)
- **An account on the [Discord Developer Portal](https://discord.com/developers/applications)** to create the bot and get the token
- **API Keys**:
  - [Steam Web API Key](https://steamcommunity.com/dev/apikey) — required
  - FACEIT API Key — optional
- **Git** installed

---

## 🚀 Installation — from scratch

### Step 1: Clone the repository

```bash
git clone https://github.com/brunao-souza/mixbot-cs2.git
cd mixbot-cs2
```

### Step 2: Create and activate a virtual environment

```bash
# Linux / macOS
python3 -m venv venv
source venv/bin/activate

# Windows
python -m venv venv
venv\Scripts\activate
```

### Step 3: Install dependencies

```bash
pip install -r requirements.txt
```

### Step 4: Create the bot in the Discord Developer Portal

1. Go to [https://discord.com/developers/applications](https://discord.com/developers/applications)
2. Click **New Application** and give your bot a name
3. Go to the **Bot** tab and click **Add Bot**
4. Copy the generated **TOKEN** — you'll put it in `.env`
5. In the same tab, enable the **Privileged Gateway Intents**:
   - `Server Members Intent`
   - `Message Content Intent`
6. Go to **OAuth2 > URL Generator**:
   - Check the scopes: `bot` and `applications.commands`
   - Check the **Administrator** permission (or select the minimum required permissions)
   - Copy the generated URL and open it in your browser to invite the bot to your Discord server

### Step 5: Configure the MySQL database

Connect to MySQL and run:

```bash
mysql -u root -p
```

```sql
CREATE DATABASE bot CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'mixbot'@'localhost' IDENTIFIED BY 'your_password_here';
GRANT ALL PRIVILEGES ON bot.* TO 'mixbot'@'localhost';
FLUSH PRIVILEGES;
EXIT;
```

> **Note:** Database tables are **created automatically** on the first bot execution — no need to run any SQL scripts manually.

### Step 6: Configure the `.env` file

```bash
cp .env.example .env
```

Open the `.env` file in an editor and fill in all the values. The **minimum** variables for the bot to work are:

| Variable | What to enter |
|---|---|
| `DISCORD_BOT_TOKEN` | Bot token (Step 4) |
| `DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` | MySQL credentials (Step 5) |
| `STEAM_API_KEY` | Your [Steam Web API](https://steamcommunity.com/dev/apikey) key |
| `RCON_HOST` / `RCON_PORT` / `RCON_PASSWORD` | CS2 server RCON IP, port and password |
| `CANAL_*_ID` and `*_ROLE_ID` | IDs of your Discord server's channels and roles |

> **How to get Discord IDs:** Enable **Developer Mode** (Settings > Advanced > Developer Mode), right-click on channels, roles or users and select **Copy ID**.

The `.env.example` file contains the complete list with explanatory comments — refer to it for details on each variable.

### Step 7: Run the bot

```bash
python main.py
```

On startup, the bot:
1. Starts a lightweight **web server** (aiohttp) on the port defined in `PORT` (default: `10000`) — used for health checks on platforms like Render
2. Connects to the MySQL database and creates tables automatically
3. Connects to Discord and syncs slash commands

To check if everything is working, open `http://localhost:10000/health` in your browser — it should return `Bot is running correctly!`.

---

## 🎮 Setting up the CS2 server (prerequisite)

The bot depends on a **Counter-Strike 2** server with the following requirements:

1. ** [MatchZy](https://github.com/shobhitp/MatchZy) plugin installed** — it manages matches, stats and webhooks
2. **RCON enabled** — the bot uses RCON to communicate with the server
3. **RCON port** — usually the same as the server port (e.g. 26849) or a specific one
4. **GOTV port** — for game broadcast
5. **`MATCHZY_WEBHOOK_KEY`** configured — must be the same on the CS2 server and in the bot's `.env` file (MatchZy sends events to the bot via HTTP)

Refer to the [MatchZy official documentation](https://github.com/shobhitp/MatchZy) for detailed installation and configuration instructions.

> ⚠️ The bot **does not manage** CS2 server installation or maintenance. You need a server running MatchZy before using the bot.

---

## 📜 Commands

The bot uses **slash commands** (`/command`). Below are the main ones grouped by category:

### 👑 Administration
| Command | Description |
|---|---|
| `/admin` | Admin panel (clear queue, reset, etc.) |
| `/config` | View/change server settings |

### 🎮 Queue and Mix
| Command | Description |
|---|---|
| `/fila` | Shows the current player queue |
| `/startmix` | Starts the mix manually (if there are 10 players) |
| `/profile` | Your profile with complete stats |

### 📊 Ranking and Stats
| Command | Description |
|---|---|
| `/ranking` | Top ELO ranking players |
| `/profile [@player]` | Detailed stats of a player |
| `/history [@player]` | Player's latest matches |

### 🔗 Steam / Registration
| Command | Description |
|---|---|
| `/register` | Opens the modal to link SteamID and nickname |
| `/steam` | Steam-related commands |

### ⚠️ Reports and Punishments
| Command | Description |
|---|---|
| `/denunciar` | Starts a report against a player |
| `/punicoes` | Punishment panel |

### 🏆 Tournament
| Command | Description |
|---|---|
| `/torneio` | Commands to manage tournaments |

### 💳 VIP
| Command | Description |
|---|---|
| `/vip` | VIP system commands |

### ℹ️ Others
| Command | Description |
|---|---|
| `/ping` | Bot latency |
| `/help` | Quick guide on how to play |
| `/groups` | Community group links |

> For the complete and up-to-date command list, check the files in `bot/cogs/`.

---

## 🚢 Deploy

### Local / VPS

To run on a VPS or dedicated server:

```bash
python main.py
```

To keep the bot running in the background, you can use:

- **systemd** (Linux) — example unit:

```ini
[Unit]
Description=MixBot Discord
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/mixbot-cs2
ExecStart=/home/ubuntu/mixbot-cs2/venv/bin/python main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

- **tmux** or **screen** — simpler solutions to keep the process active
- **pm2** — Node.js process manager (can run Python processes via `pm2 start python -- main.py`)

### Cloud platforms (Render, Railway, etc.)

The bot already includes a **health check web server** (aiohttp) on the port configured via `PORT` (default: 10000). To deploy:

1. Create a **Web Service** on the platform
2. Start command: `python main.py`
3. Set **all environment variables** (based on `.env.example`) in the platform dashboard
4. The platform will ping the `/health` endpoint to keep the service active

---

## 🐛 Troubleshooting

### Bot won't connect to Discord
- Check if `DISCORD_BOT_TOKEN` is correct in `.env`
- Confirm the bot was invited to the server with the correct intents (Server Members, Message Content)

### MySQL connection error
- Confirm MySQL is running and accessible
- Check `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD` in `.env`
- Test the connection manually: `mysql -h host -u user -p`

### Slash commands don't appear in Discord
- It may take a few minutes to sync after the first run
- The bot syncs commands per guild automatically on startup
- If they don't appear, try restarting the bot or kick/invite again

### RCON error with CS2 server
- Check if `RCON_HOST`, `RCON_PORT` and `RCON_PASSWORD` are correct
- Confirm the CS2 server is online and RCON is enabled
- Test the RCON connection manually with a tool like [rcon-cli](https://github.com/gorcon/rcon-cli)

### Bot doesn't speak in channels
- Check the bot's permissions on the Discord server
- Confirm the channel IDs (`CANAL_*_ID`) are correct
- The bot needs permission to **View Channel**, **Send Messages** and **Embed Links**

---

## 🤝 Contributing

Contributions are welcome! The project is maintained in English.

1. **Fork** the repository
2. Create a branch: `git checkout -b feature/my-feature`
3. Make your changes and commit: `git commit -m 'Add my feature'`
4. Push to GitHub: `git push origin feature/my-feature`
5. Open a **Pull Request**

---

## 📄 License

This project is distributed as open source. See the `LICENSE` file for more information (recommendation: MIT license).

---

## ⚠️ Disclaimer

This bot was extracted from a production environment and genericized for publication. Some features may require additional configuration:

- **VIP via Stripe** — requires a Stripe account and webhook configuration
- **Tournaments** — requires manual bracket configuration
- **FTP Sync** — not included in the public repository
- **Cloud platform integration** — the health check is present, but each platform has its own specifics

The **core system** (queue, mix, ranking, RCON, stats) works with just **Discord + MySQL + a CS2 server with MatchZy**.
