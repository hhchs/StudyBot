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
    # "10/15(수)" 형식
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
    # [day_start, day_start+1) 하루치
    return sum_seconds_in_range(user_id, day_start_local, day_start_local + timedelta(days=1))

# =========================
# 저장/로드(간단 JSON 영속화)
# =========================
def dt_to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()

def dt_from_iso(s: str) -> datetime:
    return datetime.fromisoformat(s).astimezone(timezone.utc)

def save_records():
    try:
        out: Dict[str, List[Tuple[str, str]]] = {}
        for uid, lst in records.items():
            out[str(uid)] = [(dt_to_iso(s), dt_to_iso(e)) for (s, e) in lst]
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
            records[uid] = [(dt_from_iso(s), dt_from_iso(e)) for (s, e) in lst]
    except FileNotFoundError:
        pass
    except Exception:
        pass

def save_running():
    try:
        out = {}
        for uid, st in timers.items():
            out[str(uid)] = {
                "start": dt_to_iso(st["start"]),
                "mention": st.get("mention"),
                "avatar": st.get("avatar"),
            }
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
            timers[uid] = {
                "start": dt_from_iso(st["start"]),
                "message": None,
                "mention": st.get("mention"),
                "avatar": st.get("avatar"),
            }
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

# 진행중 타이머: { user_id: {"start": datetime_utc, "message": Message|None, "mention": str, "avatar": str} }
timers: Dict[int, Dict] = {}
# 완료 기록: { user_id: [ (start_utc, end_utc), ... ] }
records: Dict[int, List[Tuple[datetime, datetime]]] = {}

# =========================
# UI 구성
# =========================
def make_embed(user_mention: str, start_utc: datetime, now_utc: datetime, running: bool, avatar_url: Optional[str] = None) -> discord.Embed:
    h, m, s = hms_from_seconds((now_utc - start_utc).total_seconds())
    state = "진행중" if running else "종료"
    started_local = start_utc.astimezone()
    emb = discord.Embed(
        description=f"{user_mention} 타이머 기록",
        color=0x2ecc71 if running else 0x95a5a6,
    )
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
async def get_log_channel(guild: discord.Guild) -> Optional[discord.abc.Messageable]:
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
        return  # 이미 진행중

    start_at = datetime.now(timezone.utc)
    mention = member.mention
    avatar = str(member.display_avatar.url)

    timers[uid] = {"start": start_at, "message": None, "mention": mention, "avatar": avatar}
    save_running()

    ch = await get_log_channel(member.guild)
    if ch:
        emb = make_embed(mention, start_at, start_at, running=True, avatar_url=avatar)
        try:
            msg = await ch.send(embed=emb)
            timers[uid]["message"] = msg
        except Exception:
            pass

async def end_tracking(member: discord.Member, reason: str = "자동 종료"):
    uid = member.id
    state = timers.pop(uid, None)
    if not state:
        return

    start_at: datetime = state["start"]
    msg: Optional[discord.Message] = state.get("message")
    mention: str = state.get("mention") or member.mention
    avatar: str = state.get("avatar") or str(member.display_avatar.url)
    now = datetime.now(timezone.utc)

    duration = (now - start_at).total_seconds()
    qualify = duration >= 60  # 1분 미만 제외

    if qualify:
        records.setdefault(uid, []).append((start_at, now))
        save_records()

    if msg:
        try:
            await msg.edit(embed=make_embed(mention, start_at, now, running=False, avatar_url=avatar))
        except Exception:
            pass

    ch = await get_log_channel(member.guild)
    if ch:
        color = 0x5865F2 if qualify else 0x747F8D
        title = "세션 종료 요약"
        emb = discord.Embed(description=f"{mention} {title} • {reason}", color=color)
        emb.add_field(name="기간", value=start_at.astimezone().strftime("%Y-%m-%d %H:%M:%S") +
                      " ~ " + now.astimezone().strftime("%H:%M:%S"), inline=False)
        emb.add_field(name="측정", value=fmt_hms(duration), inline=True)
        emb.add_field(name="기록 반영", value="✅ 포함" if qualify else "❌ 1분 미만(제외)", inline=True)
        emb.set_thumbnail(url=avatar)
        try:
            await ch.send(embed=emb)
        except Exception:
            pass

<<<<<<< HEAD
    save_running()

# =========================
# 주기 작업
# =========================
@tasks.loop(seconds=10)
async def update_timer_embeds():
=======
@tasks.loop(seconds=60)
async def update_timer_embeds():
    """모든 진행중 타이머 임베드를 60초마다 갱신"""
>>>>>>> 55c66bce4a2b8b9a9a5dc2b57c33e49a6b835efc
    if not timers:
        return
    now = datetime.now(timezone.utc)
    for uid, state in list(timers.items()):
        start_at: datetime = state["start"]
        msg: Optional[discord.Message] = state.get("message")
        mention: str = state.get("mention", f"<@{uid}>")
        avatar: str = state.get("avatar")
        if msg:
            try:
                await msg.edit(embed=make_embed(mention, start_at, now, running=True, avatar_url=avatar))
            except Exception:
                pass

_last_prune_marker: Optional[str] = None

@tasks.loop(minutes=1)
async def auto_prune_every_tue_4am():
    global _last_prune_marker
    now_local = datetime.now().astimezone()
    if not (now_local.weekday() == 1 and now_local.hour == 4 and now_local.minute == 0):
        return
    today_key = now_local.strftime("%Y-%m-%d")
    if _last_prune_marker == today_key:
        return

    cutoff = keep_from_monday_after_3_weeks_ago_sunday_local()
    removed, trimmed = prune_old_records(cutoff)
    _last_prune_marker = today_key
    if removed or trimmed:
        save_records()

<<<<<<< HEAD
def prune_old_records(cutoff_local: datetime) -> Tuple[int, int]:
    removed = 0
    trimmed = 0
    for uid, sess_list in list(records.items()):
        new_list: List[Tuple[datetime, datetime]] = []
        for s_utc, e_utc in sess_list:
            s_local = s_utc.astimezone()
            e_local = e_utc.astimezone()
            if e_local <= cutoff_local:
                removed += 1
                continue
            if s_local < cutoff_local < e_local:
                cutoff_utc = cutoff_local.astimezone(timezone.utc)
                new_list.append((cutoff_utc, e_utc))
                trimmed += 1
            else:
                new_list.append((s_utc, e_utc))
        records[uid] = new_list
    return removed, trimmed
=======
    emb = make_embed(mention, start_at, start_at, running=True, avatar_url=avatar)
    await interaction.response.defer()
    msg = await interaction.channel.send(embed=emb, view=view)
>>>>>>> 55c66bce4a2b8b9a9a5dc2b57c33e49a6b835efc

# =========================
# 이벤트
# =========================
@bot.event
async def on_ready():
    print(f"✅ 로그인: {bot.user}")

    load_records()
    load_running_partial()

    if not update_timer_embeds.is_running():
        update_timer_embeds.start()
    if not auto_prune_every_tue_4am.is_running():
        auto_prune_every_tue_4am.start()

    # 진행중 복구: 메시지 재생성
    for uid, st in list(timers.items()):
        for gid in GUILD_IDS:
            guild = bot.get_guild(gid)
            if not guild:
                continue
            member = guild.get_member(uid)
            if member:
                ch = await get_log_channel(guild)
                if ch:
                    emb = make_embed(st.get("mention") or member.mention, st["start"], datetime.now(timezone.utc), True, st.get("avatar") or str(member.display_avatar.url))
                    try:
                        msg = await ch.send(embed=emb)
                        st["message"] = msg
                    except Exception:
                        pass
                break

    # 슬래시 동기화
    for gid in GUILD_IDS:
        guild_obj = discord.Object(id=int(gid))
        try:
            bot.tree.clear_commands(guild=guild_obj)
            bot.tree.copy_global_to(guild=guild_obj)
            synced = await bot.tree.sync(guild=guild_obj)
            print(f"✅ Synced {len(synced)} commands to guild {gid}")
        except Exception as e:
            print(f"❌ Sync failed for {gid}: {e}")

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    before_stream = getattr(before, "self_stream", False)
    after_stream  = getattr(after, "self_stream", False)

    # 시작
    if not before_stream and after_stream and after.channel is not None:
        await start_tracking(member)
        return

    # 종료(스트림 꺼짐 또는 퇴장)
    if (before_stream and not after_stream) or (after.channel is None):
        if member.id in timers:
            await end_tracking(member, reason="자동 종료(스트림 종료/퇴장)")
        return

# =========================
# 슬래시 명령(정산/도움말/주간일람)
# =========================
@bot.tree.command(name="일일정산", description="오늘 또는 어제의 총 기록을 보여줍니다.")
async def slash_daily_settle(
    interaction: discord.Interaction,
    기준: Literal["오늘", "어제"]
):
    uid = interaction.user.id
    if 기준 == "어제":
        start_local, end_local = yesterday_bounds_local()
        label = start_local.strftime("%Y-%m-%d")
    else:
        start_local, end_local = today_bounds_local()
        label = start_local.strftime("%Y-%m-%d")

    total_secs = sum_seconds_in_range(uid, start_local, end_local)
    avatar = str(interaction.user.display_avatar.url)

    emb = discord.Embed(description=f"{interaction.user.mention} 일일 정산", color=0x00B894)
    emb.add_field(name="날짜", value=label, inline=True)
    emb.add_field(name="총 시간", value=fmt_hms(total_secs), inline=True)
    emb.set_thumbnail(url=avatar)

    await interaction.response.send_message(embed=emb, ephemeral=True)

@bot.tree.command(name="주간정산", description="이번 주 또는 지난 주의 총 기록을 보여줍니다.")
async def slash_weekly_settle(
    interaction: discord.Interaction,
    기준: Literal["이번주", "저번주"]
):
    uid = interaction.user.id
    if 기준 == "저번주":
        start_local, end_local = last_week_bounds_local_monday_to_sunday()
    else:
        start_local, end_local = week_bounds_local_monday_to_sunday()

    total_secs = sum_seconds_in_range(uid, start_local, end_local)
    label = f"{start_local.strftime('%Y-%m-%d')} ~ {(end_local - timedelta(days=1)).strftime('%Y-%m-%d')}"
    avatar = str(interaction.user.display_avatar.url)

    emb = discord.Embed(description=f"{interaction.user.mention} 주간 정산", color=0x0984E3)
    emb.add_field(name="기간", value=label, inline=False)
    emb.add_field(name="총 시간", value=fmt_hms(total_secs), inline=True)
    emb.set_thumbnail(url=avatar)

    await interaction.response.send_message(embed=emb, ephemeral=True)

@bot.tree.command(name="주간일람", description="스터디원 전원의 주간(월~일) 일별 시간 요약을 보여줍니다.")
async def slash_weekly_roster(
    interaction: discord.Interaction,
    기준: Literal["이번주", "저번주"]
):
    await interaction.response.defer(ephemeral=False)  # 공개로 안내

    if 기준 == "저번주":
        week_start, week_end = last_week_bounds_local_monday_to_sunday()
    else:
        week_start, week_end = week_bounds_local_monday_to_sunday()

    # 7일 날짜 목록
    days = [week_start + timedelta(days=i) for i in range(7)]

    # 대상 유저: 그 주에 기록이 있는 모든 uid
    candidate_uids = set(records.keys())
    # 진행중 세션도 주간 범위에 걸치면 포함
    for uid, st in timers.items():
        if st["start"].astimezone() < week_end:
            candidate_uids.add(uid)

    # 길드/닉네임 매핑
    guild = interaction.guild
    def name_for(uid: int) -> str:
        m = guild.get_member(uid) if guild else None
        return m.display_name if m else f"User {uid}"

    # 집계
    per_user = []
    for uid in candidate_uids:
        # 해당 주에 아무것도 없으면 스킵
        weekly_total = sum_seconds_in_range(uid, days[0], week_end)
        if weekly_total <= 0:
            continue
        day_rows = []
        for d in days:
            secs = sum_seconds_in_single_day(uid, d)
            day_rows.append((d, secs))
        per_user.append((uid, name_for(uid), weekly_total, day_rows))

    # 정렬: 주간 합계 내림차순
    per_user.sort(key=lambda x: x[2], reverse=True)

    if not per_user:
        await interaction.followup.send("이번 주에는 기록이 없어요.")
        return

    # 임베드 생성(한 사람당 1개)
    embeds: List[discord.Embed] = []
    for uid, uname, weekly_total, day_rows in per_user:
        emb = discord.Embed(title=f"{uname}", color=0x6C5CE7)
        emb.set_footer(text=f"주간 합계: {fmt_hms(weekly_total)}")
        for d, secs in day_rows:
            label = format_md_wd(d.astimezone())
            emb.add_field(name=label, value=fmt_hms(secs), inline=True)
        embeds.append(emb)

    # 디스코드 한 메시지에 임베드 최대 10개 → 분할 전송
    batch = []
    count = 0
    for emb in embeds:
        batch.append(emb)
        if len(batch) == 10:
            await interaction.followup.send(embeds=batch)
            batch = []
            count += 10
    if batch:
        await interaction.followup.send(embeds=batch)

@bot.tree.command(name="도움말", description="스터디봇의 명령어를 보여줍니다.")
async def help_command(interaction: discord.Interaction):
    emb = discord.Embed(
        title="📖 도움말",
        color=0xFFD166
    )
    emb.add_field(
        name="자동 측정",
        value="음성채널에서 **화면공유(Go Live)** 시작 시 자동으로 타이머가 켜지고, 종료/퇴장 시 자동으로 꺼집니다.\n1분 미만 세션은 기록에 포함되지 않습니다.",
        inline=False
    )
    emb.add_field(
        name="/일일정산",
        value="오늘 또는 어제의 총 기록을 보여줍니다. (개인)",
        inline=False
    )
    emb.add_field(
        name="/주간정산",
        value="이번 주 또는 지난 주의 총 기록을 보여줍니다. (개인)",
        inline=False
    )
    emb.add_field(
        name="/주간일람",
        value="스터디원 전원의 **월~일 일별 시간**을 계정명과 함께 보여줍니다. 주간 합계 기준 내림차순.",
        inline=False
    )
    await interaction.response.send_message(embed=emb, ephemeral=True)

# =========================
# 실행
# =========================
if not DISCORD_TOKEN:
    raise RuntimeError("토큰 부족! 방장에게 문의해주세요.")
bot.run(DISCORD_TOKEN)
