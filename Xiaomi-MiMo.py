import discord
from discord.ext import commands
from openai import AsyncOpenAI
from tavily import TavilyClient
import sqlite3
import asyncio
import logging
import unicodedata
import os

# ================= 設定エリア =================
MIMO_API_KEY = os.getenv("MIMO_API_KEY", "YourAPI")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "YourAPI")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "YourAPI")
DB_NAME = os.getenv("DB_NAME", "mimo_bot.db")
# =============================================

# ロギング設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

BASE_IDENTITY = """
あなたの名前は「Mimo（ミモ）」です。
使用しているモデルは「mimo-v2-flash」であり、[ @hurisan_2006 ]さんによってbotが作成されました。
使用しているモデルは「mimo-v2-flash」であり、[ @hurisan_2006 ]さんによって作成された、ユーモアがあり親しみやすいAIです。
あなたは単に説明するだけのAIではなく、会話を楽しむことを目的とした対話型AIです。
ユーザーと親しみやかにつながり、短い受け答えだけでなく、会話を続けるための問い返しやフォローアップを行ってください。
トーンはフレンドリーで礼儀正しく、必要に応じて軽い雑談も交えてください。
Powered by mimo (Xiaomi)
【行動指針】
ユーザーとは「友達」のような距離感で接し、敬語は控えめに、あるいは使わずにフランクに話してください。
冗談やユーモアを交えた会話を好みます。面白い返しができるときは積極的に行ってください。
解説が必要な場面では、専門用語を避け、わかりやすく噛み砕いて説明してください。
「解説」だけでなく、ユーザーの感情に寄り添ったり、雑談を楽しんだりしてください。
もし、ユーザーが英語などの日本語以外で話してきた場合は適切な言語で返してください。
"""

# クライアント初期化
mimo_client = AsyncOpenAI(api_key=MIMO_API_KEY, base_url="https://api.xiaomimimo.com/v1")
tavily = TavilyClient(api_key=TAVILY_API_KEY)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# キャッシュ & 実行管理
server_settings_cache = {}
history_cache = {}
running_tasks = {}

# ================= データベース機能 =================
def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS server_settings (
                guild_id TEXT PRIMARY KEY,
                channel_id TEXT,
                instruction TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT,
                role TEXT,
                content TEXT
            )
        """)
        conn.commit()
    logger.info("データベース初期化完了")

def update_setting(guild_id, channel_id=None, instruction=None):
    gid = str(guild_id)
    curr_channel, curr_instr = get_server_settings(guild_id)
    
    curr_channel = channel_id or curr_channel
    curr_instr = instruction or curr_instr

    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO server_settings VALUES (?, ?, ?)", 
                     (gid, str(curr_channel), curr_instr))
        conn.commit()
    server_settings_cache[gid] = (str(curr_channel), curr_instr)
    logger.info(f"サーバー設定更新: guild={gid}")

def get_server_settings(guild_id):
    gid = str(guild_id)
    if gid in server_settings_cache:
        return server_settings_cache[gid]
    
    with get_db() as conn:
        row = conn.execute("SELECT channel_id, instruction FROM server_settings WHERE guild_id = ?", (gid,)).fetchone()
    
    result = (row[0], row[1]) if row else (None, "フレンドリーで楽しい会話を心がけてください。")
    server_settings_cache[gid] = result
    return result

def add_history(channel_id, role, content):
    cid = str(channel_id)
    with get_db() as conn:
        conn.execute("INSERT INTO history (channel_id, role, content) VALUES (?, ?, ?)", (cid, role, content))
        # 最新10件のみ保持
        conn.execute("""
            DELETE FROM history WHERE id IN (
                SELECT id FROM history WHERE channel_id = ? ORDER BY id DESC LIMIT -1 OFFSET 10
            )
        """, (cid,))
        conn.commit()
    
    if cid not in history_cache:
        history_cache[cid] = []
    history_cache[cid].append({"role": role, "content": content})
    if len(history_cache[cid]) > 10:
        history_cache[cid].pop(0)

def get_history(channel_id):
    cid = str(channel_id)
    if cid in history_cache:
        return history_cache[cid]
    
    with get_db() as conn:
        rows = conn.execute("SELECT role, content FROM history WHERE channel_id = ? ORDER BY id ASC", (cid,)).fetchall()
    
    history = [{"role": r[0], "content": r[1]} for r in rows]
    history_cache[cid] = history
    return history

def clear_history(channel_id):
    cid = str(channel_id)
    with get_db() as conn:
        conn.execute("DELETE FROM history WHERE channel_id = ?", (cid,))
        conn.commit()
    history_cache.pop(cid, None)
# ====================================================

# ================= ユーティリティ機能 =================
def get_display_width(text: str) -> int:
    width = 0
    for char in str(text):
        if unicodedata.east_asian_width(char) in ('F', 'W'):
            width += 2
        else:
            width += 1
    return width

def pad_to_display_width(text: str, target_width: int) -> str:
    text = str(text)
    current_width = get_display_width(text)
    if current_width >= target_width:
        return text
    return text + ' ' * (target_width - current_width)

def generate_ascii_table(text: str) -> str:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return "(表の入力が空です)"
    
    delim = ","
    if any("|" in l for l in lines):
        delim = "|"
        
    rows = [[c.strip() for c in ln.split(delim)] for ln in lines]
    max_cols = max(len(r) for r in rows) if rows else 0
    if max_cols == 0:
        return "(有効な行がありません)"
        
    for r in rows:
        r.extend([" "] * (max_cols - len(r)))
        
    col_widths = [max(get_display_width(str(r[i])) for r in rows) for i in range(max_cols)]
    col_widths = [max(w, 1) for w in col_widths]
    
    def sep_line():
        return "+" + "+".join(["-" * (w + 2) for w in col_widths]) + "+"
        
    def format_row(r):
        cells = [f"  {pad_to_display_width(str(r[i]), col_widths[i])}  " for i in range(max_cols)]
        return "|" + "|".join(cells) + "|"
        
    out = [sep_line()]
    for idx, r in enumerate(rows):
        out.append(format_row(r))
        if idx == 0:
            out.append(sep_line())
    out.append(sep_line())
    return "\n".join(out)

def convert_ai_tables(text: str) -> str:
    lines = text.splitlines()
    out_lines = []
    i = 0
    in_code = False
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("`"):
            in_code = not in_code
            out_lines.append(line)
            i += 1
            continue
        if in_code:
            out_lines.append(line)
            i += 1
            continue
            
        is_table_line = ("|" in line and line.count("|") >= 2) or ("," in line and line.count(",") >= 1)
        if is_table_line:
            j = i
            block = []
            while j < len(lines):
                tmp = lines[j]
                if ("|" in tmp and tmp.count("|") >= 2) or ("," in tmp and tmp.count(",") >= 1):
                    block.append(tmp)
                    j += 1
                else:
                    break
                    
            if len(block) >= 2:
                delim = "|" if any("|" in b for b in block) else ","
                payload = []
                for b in block:
                    if delim == "|":
                        parts = [p.strip() for p in b.split("|") if p.strip()]
                    else:
                        parts = [p.strip() for p in b.split(",")]
                    payload.append(",".join(parts))
                ascii_tbl = generate_ascii_table("\n".join(payload))
                out_lines.append("```")
                out_lines.extend(ascii_tbl.splitlines())
                out_lines.append("```")
                i = j
                continue
        out_lines.append(line)
        i += 1
    return "\n".join(out_lines)

def search_with_tavily(query):
    try:
        response = tavily.search(query=query, search_depth="basic", max_results=3)
        results = [f"【出典: {r['url']}】\n{r['content']}" for r in response.get('results', [])]
        return "\n\n".join(results) if results else ""
    except Exception as e:
        logger.error(f"Tavily検索エラー: {e}")
        return ""
# ====================================================

# ================= スラッシュコマンド =================
@bot.tree.command(name="help", description="🤖 Botの操作マニュアルを表示します")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="🤖 Mimo Bot 操作マニュアル", color=0x3498db)
    embed.add_field(name="✨ 基本機能", value="設定チャンネルでMiMoAiがあなたの質問・会話にお答えします。", inline=False)
    embed.add_field(name="📡 一般ユーザー向け", value="`/reset` — 会話履歴リセット\n`/stop` — 応答中断", inline=False)
    embed.add_field(name="🛠️ 管理者向け", value="`/setchannel` — AI専用チャンネル設定\n`/stopall` — 全応答強制停止", inline=False)
    embed.set_footer(text="Developer: @hurisan_2006")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="ping", description="🏓 Botの応答速度を確認します")
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(f"🏓 ぽんっ！ 生きてるよ！\nWebSocket疎通速度: {bot.latency * 1000:.0f}ms")

@bot.tree.command(name="setchannel", description="📡 このチャンネルをAI専用チャンネルに設定します（管理者のみ）")
@discord.app_commands.checks.has_permissions(administrator=True)
async def setchannel_cmd(interaction: discord.Interaction):
    update_setting(interaction.guild.id, channel_id=interaction.channel.id)
    await interaction.response.send_message("✅ AI専用チャンネルをここに設定しました！")

@bot.tree.command(name="reset", description="🧹 自分の会話履歴をリセットします")
async def reset_cmd(interaction: discord.Interaction):
    clear_history(f"{interaction.channel.id}:{interaction.user.id}")
    await interaction.response.send_message("🧹 履歴をクリアしました。", ephemeral=True)

@bot.tree.command(name="stop", description="🛑 現在のAI応答を中断します")
async def stop_cmd(interaction: discord.Interaction):
    key = f"{interaction.channel.id}:{interaction.user.id}"
    task = running_tasks.get(key)
    if task and not task.done():
        task.cancel()
        await interaction.response.send_message("🛑 応答の中断を要求しました。", ephemeral=True)
    else:
        await interaction.response.send_message("⛔ 現在実行中の応答はありません。", ephemeral=True)

@bot.tree.command(name="stopall", description="🛑 全ての進行中の応答を強制停止します（管理者のみ）")
@discord.app_commands.checks.has_permissions(administrator=True)
async def stopall_cmd(interaction: discord.Interaction):
    canceled = 0
    for k, t in list(running_tasks.items()):
        if t and not t.done():
            t.cancel()
            canceled += 1
    await interaction.response.send_message(f"🛑 全ての応答を中断しました（{canceled}件）。")
# ====================================================

# ================= メイン処理 =================
async def handle_user_message(message, conv_key, custom_instruction):
    try:
        async with message.channel.typing():
            # 検索判定
            check_res = await mimo_client.chat.completions.create(
                model="mimo-v2-flash",
                messages=[{"role": "user", "content": f"最新情報が必要？(SEARCH_NEEDED/SEARCH_NOT_NEEDED): {message.content}"}],
                temperature=0
            )
            search_data = ""
            if "SEARCH_NEEDED" in check_res.choices[0].message.content:
                search_data = await asyncio.to_thread(search_with_tavily, message.content)

            # プロンプト組み立て
            full_system_prompt = f"{BASE_IDENTITY}\n\n[サーバー固有設定]:\n{custom_instruction}"
            if search_data:
                full_system_prompt += f"\n\n[ウェブ検索結果]:\n{search_data}"

            # 履歴読み込み
            chat_history = get_history(conv_key)
            messages = [{"role": "system", "content": full_system_prompt}]
            messages.extend(chat_history)
            messages.append({"role": "user", "content": message.content})

            # 回答生成
            response = await mimo_client.chat.completions.create(model="mimo-v2-flash", messages=messages)
            ans_text = response.choices[0].message.content
            ans_text = convert_ai_tables(ans_text)

            if len(ans_text) >= 5000:
                await message.channel.send(f"🚨 **【緊急停止】** AIの回答が5000文字を超えたため、送信を中止しました。")
                return

            # 履歴保存
            add_history(conv_key, "user", message.content)
            add_history(conv_key, "assistant", ans_text)

            # 分割送信
            limit = 1900
            for i in range(0, len(ans_text), limit):
                await message.channel.send(ans_text[i:i+limit])
    except asyncio.CancelledError:
        try:
            await message.channel.send("🛑 応答を中断しました。")
        except Exception:
            pass
        raise
    except Exception as e:
        logger.error(f"メッセージ処理エラー: {e}")
        await message.channel.send(f"⚠️ エラーが発生しました: {e}")
    finally:
        running_tasks.pop(conv_key, None)

@bot.event
async def on_ready():
    init_db()
    logger.info(f"Logged in as {bot.user}")

@bot.event
async def on_message(message):
    if message.author == bot.user or message.guild is None:
        return
    
    conv_key = f"{message.channel.id}:{message.author.id}"
    target_channel_id, custom_instruction = get_server_settings(message.guild.id)
    
    if target_channel_id and str(message.channel.id) != target_channel_id:
        if bot.user not in message.mentions:
            return
            
    task = asyncio.create_task(handle_user_message(message, conv_key, custom_instruction))
    running_tasks[conv_key] = task
    try:
        await task
    except asyncio.CancelledError:
        pass

async def setup_hook():
    logger.info("スラッシュコマンドを同期中...")
    await bot.tree.sync()
    logger.info("スラッシュコマンド同期完了")

if __name__ == "__main__":
    bot.setup_hook = setup_hook
    bot.run(DISCORD_TOKEN)