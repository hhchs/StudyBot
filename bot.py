# bot.py — 화면공유 자동 타이머(1분 미만 제외) + 주간 일람(전원)
# Python 3.13.7
import os
import json
import discord
from discord.ext import commands, tasks
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from typing import Dict, List, Tuple, Optional
from typing import Literal

# =========================
# 환경설정
# =========================
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_IDS = [int(x) for x in os.getenv("GUILD_IDS","").split(",") if x.strip()]
STREAM_LOG_CHANNEL_ID = int(os.getenv("STREAM_LOG_CHANNEL_ID", "0"))  # 선택

DATA_DIR = os.getenv("DATA_DIR", "./data")
RECORDS_JSON = os.path.join(DATA_DIR, "records.json")   # 완료 세션
RUNNING_JSON = os.path.join(DATA_DIR, "running.json")   # 진행중 세션(복구용)
os.makedirs(DATA_DIR, exist_ok=True)

# =========================
# 시간 유틸
# =========================
KOR_WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]

def format_md_wd(dt_local: datetime) -> str:
    return dt_local.strftime("%m/%d") + f"({KOR_WEEKDAYS[dt_local.weekday()]})"

def yesterday_bounds_local():
    now_local = datetime.now().astimezone()
    today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    y_start = today_start - timedelta(days=1)
    y_end = today_start
    return y_start, y_end

def last_week_bounds_local_monday_to_sunday():
    now_local = datetime.now().astimezone()
    this_monday = (now_local - timedelta(days=now_local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    last_monday = this_monday - timedelta(days=7)
    return last_monday, this_monday

def today_bounds_local() -> Tuple[datetime, datetime]:
    now_local = datetime.now().astimezone()
    start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start, end

def week_bounds_local_monday_to_sunday() -> Tuple[datetime, datetime]:
    now_local = datetime.now().astimezone()
    monday = (now_local - timedelta(days=now_local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    next_monday = monday + timedelta(days=7)
    return monday, next_monday

def keep_from_monday_after_3_weeks_ago_sunday_local() -> datetime:
    now_local = datetime.now().astimezone()
    three_weeks_ago = now_local - timedelta(weeks=3)
    monday_of_that_week = (three_weeks_ago - timedelta(days=three_weeks_ago.weekday())) \
        .replace(hour=0, minute=0, second=0, microsecond=0)
    keep_from_monday = monday_of_that_week + timedelta(days=7)
    return keep_from_monday

# =========================
# 합계/포맷 유틸
# =========================
def hms_from_seconds(secs: float) -> Tuple[int, int, int]:
    t = int(secs)
    return t // 3600, (t % 3600) // 60, t % 60

def fmt_hms(secs: float) -> str:
    h, m, s = hms_from_seconds(secs)
    return f"{h:02d}:{m:02d}:{s:02d}"

def sum_seconds_in_range(user_id: int, range_start_local: datetime, range_end_local: datetime) -> float:
    def overlap_seconds(a1: datetime, a2: datetime, b1: datetime, b2: datetime) -> float:
        start = max(a1, b1)
        end = min(a2, b2)
        return max(0.0, (end - start).total_seconds())

    total = 0.0
    for (s_utc, e_utc) in records.get(user_id, []):
        s_local = s_utc.astimezone()
        e_local = e_utc.astimezone()
        total += overlap_seconds(s_local, e_local, range_start_local, range_end_local)

    if user_id in timers:
        s_utc = timers[user_id]["start"]
        s_local = s_utc.astimezone()
        now_local = datetime.now(timezone.utc).astimezone()
        total += overlap_seconds(s_local, now_local, range_start_local, range_end_local)
    return total

def sum_seconds_in_single_day(user_id: int, day_start_local: datetime) -> float:
    return sum_seconds_in_range(user_id, day_start_local, day_start_local + timedelta(days=1))

# =========================
# 저장/로드
# =========================
def dt_to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()

def dt_from_iso(s: str) -> datetime:
    return datetime.fromisoformat(s).astimezone(timezone.utc)

def save_records():
    try:
        out = {str(uid): [(dt_to_iso(s), dt_to_iso(e)) for s, e in lst] for uid, lst in records.items()}
        with open(RECORDS_JSON, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def load_records():
    try:
        with open(RECORDS_JSON, "r", encoding="utf-8") as f:
            raw = json.load(f)
        for k, lst in raw.items():
            uid = int(k)
            records[uid] = [(dt_from_iso(s), dt_from_iso(e)) for s, e in lst]
    except FileNotFoundError:
        pass
    except Exception:
        pass

def save_running():
    try:
        out = {str(uid): {"start": dt_to_iso(st["start"]), "mention": st.get("mention"), "avatar": st.get("avatar")} for uid, st in timers.items()}
        with open(RUNNING_JSON, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def load_running_partial():
    try:
        with open(RUNNING_JSON, "r", encoding="utf-8") as f:
            raw = json.load(f)
        for k, st in raw.items():
            uid = int(k)
            timers[uid] = {"start": dt_from_iso(st["start"]), "message": None, "mention": st.get("mention"), "avatar": st.get("avatar")}
    except FileNotFoundError:
        pass
    except Exception:
        pass

# =========================
# 디스코드 클라이언트
# =========================
intents = discord.Intents.default()
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)

timers: Dict[int, Dict] = {}
records: Dict[int, List[Tuple[datetime, datetime]]] = {}

# =========================
# UI 구성
# =========================
def make_embed(user_mention: str, start_utc: datetime, now_utc: datetime, running: bool, avatar_url: Optional[str] = None) -> discord.Embed:
    h, m, s = hms_from_seconds((now_utc - start_utc).total_seconds())
    state = "진행중" if running else "종료"
    started_local = start_utc.astimezone()
    emb = discord.Embed(description=f"{user_mention} 타이머 기록", color=0x2ecc71 if running else 0x95a5a6)
    emb.add_field(name="날짜", value=started_local.strftime("%Y-%m-%d"), inline=True)
    emb.add_field(name="시간", value=f"{h:02d}:{m:02d}:{s:02d}", inline=True)
    emb.add_field(name="상태", value=state, inline=True)
    emb.set_footer(text="⏱️ 1분 단위 자동 갱신")
    if avatar_url:
        emb.set_thumbnail(url=avatar_url)
    return emb

# =========================
# 로그 채널 선택
# =========================
async def get_log_channel(guild: discord.Guild):
    if STREAM_LOG_CHANNEL_ID:
        ch = guild.get_channel(STREAM_LOG_CHANNEL_ID)
        if ch:
            return ch
    if guild.system_channel:
        return guild.system_channel
    for ch in guild.text_channels:
        if ch.permissions_for(guild.me).send_messages:
            return ch
    return None

# =========================
# 시작/종료 로직
# =========================
async def start_tracking(member: discord.Member):
    uid = member.id
    if uid in timers:
        return
    start_at = datetime.now(timezone.utc)
    mention = member.mention
    avatar = str(member.display_avatar.url)
    timers[uid] = {"start": start_at, "message": None, "mention": mention, "avatar": avatar}
    save_running()
    ch = await get_log_channel(member.guild)
    if ch:
        emb = make_embed(mention, start_at, start_at, True, avatar)
        try:
            msg = await ch.send(embed=emb)
            timers[uid]["message"] = msg
        except Exception:
            pass

async def end_tracking(member: discord.Member, reason="자동 종료"):
    uid = member.id
    state = timers.pop(uid, None)
    if not state:
        return
    start_at = state["start"]
    msg = state.get("message")
    mention = state.get("mention") or member.mention
    avatar = state.get("avatar") or str(member.display_avatar.url)
    now = datetime.now(timezone.utc)
    duration = (now - start_at).total_seconds()
    qualify = duration >= 60
    if qualify:
        records.setdefault(uid, []).append((start_at, now))
        save_records()
    if msg:
        try:
            await msg.edit(embed=make_embed(mention, start_at, now, False, avatar))
        except Exception:
            pass
    ch = await get_log_channel(member.guild)
    if ch:
        color = 0x5865F2 if qualify else 0x747F8D
        emb = discord.Embed(description=f"{mention} 세션 종료 요약 • {reason}", color=color)
        emb.add_field(name="기간", value=start_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")+" ~ "+now.astimezone().strftime("%H:%M:%S"))
        emb.add_field(name="측정", value=fmt_hms(duration))
        emb.add_field(name="기록 반영", value="✅ 포함" if qualify else "❌ 1분 미만(제외)")
        emb.set_thumbnail(url=avatar)
        try:
            await ch.send(embed=emb)
        except Exception:
            pass
    save_running()

# =========================
# 주기 갱신
# =========================
@tasks.loop(seconds=60)
async def update_timer_embeds():
    if not timers:
        return
    now = datetime.now(timezone.utc)
    for uid, state in list(timers.items()):
        msg = state.get("message")
        if msg:
            try:
                await msg.edit(embed=make_embed(state["mention"], state["start"], now, True, state["avatar"]))
            except Exception:
                pass

# =========================
# 이벤트
# =========================
@bot.event
async def on_ready():
    print(f"✅ 로그인: {bot.user}")
    load_records()
    load_running_partial()
    update_timer_embeds.start()
    for gid in GUILD_IDS:
        guild = bot.get_guild(gid)
        if guild:
            print(f"🟢 서버 연결됨: {guild.name}")

@bot.event
async def on_voice_state_update(member, before, after):
    before_stream = getattr(before, "self_stream", False)
    after_stream = getattr(after, "self_stream", False)
    if not before_stream and after_stream and after.channel:
        await start_tracking(member)
    elif (before_stream and not after_stream) or (after.channel is None):
        if member.id in timers:
            await end_tracking(member, "자동 종료(스트림 종료/퇴장)")

# =========================
# 슬래시 명령
# =========================
@bot.tree.command(name="도움말")
async def help_cmd(i: discord.Interaction):
    e = discord.Embed(title="📖 도움말", color=0xFFD166)
    e.add_field(name="자동 측정", value="음성채널에서 **화면공유** 시작시 자동 기록. 1분 미만은 제외.", inline=False)
    e.add_field(name="/일일정산", value="오늘/어제 개인 총시간", inline=False)
    e.add_field(name="/주간정산", value="이번주/저번주 개인 총시간", inline=False)
    e.add_field(name="/주간일람", value="모든 스터디원 주간 일별 시간표", inline=False)
    await i.response.send_message(embed=e, ephemeral=True)

# =========================
# 실행
# =========================
if not DISCORD_TOKEN:
    raise RuntimeError("토큰 부족! 방장에게 문의해주세요.")
bot.run(DISCORD_TOKEN)
