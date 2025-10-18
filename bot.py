# bot.py — 화면공유 자동 타이머(1분 미만 제외) + 주간 일람(전원)
# Python 3.10+
import os, json, discord
from discord.ext import commands, tasks
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from typing import Dict, List, Tuple, Optional, Literal

# ---------------- 설정 ----------------
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_IDS = [int(x) for x in os.getenv("GUILD_IDS","").split(",") if x.strip()]
STREAM_LOG_CHANNEL_ID = int(os.getenv("STREAM_LOG_CHANNEL_ID","0"))
DATA_DIR = os.getenv("DATA_DIR","./data")
RECORDS_JSON = os.path.join(DATA_DIR,"records.json")
RUNNING_JSON = os.path.join(DATA_DIR,"running.json")
os.makedirs(DATA_DIR, exist_ok=True)

# ---------------- 시간 유틸 ----------------
KOR_WD = ["월","화","수","목","금","토","일"]

def format_md_wd(dt_local: datetime) -> str:
    return dt_local.strftime("%m/%d") + f"({KOR_WD[dt_local.weekday()]})"

def yesterday_bounds_local():
    now = datetime.now().astimezone()
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return today0 - timedelta(days=1), today0

def last_week_bounds_local_monday_to_sunday():
    now = datetime.now().astimezone()
    this_mon = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return this_mon - timedelta(days=7), this_mon

def today_bounds_local():
    now = datetime.now().astimezone()
    s = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return s, s + timedelta(days=1)

def week_bounds_local_monday_to_sunday():
    now = datetime.now().astimezone()
    mon = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return mon, mon + timedelta(days=7)

def keep_from_monday_after_3_weeks_ago_sunday_local():
    now = datetime.now().astimezone()
    three = now - timedelta(weeks=3)
    mon = (three - timedelta(days=three.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return mon + timedelta(days=7)

# ---------------- 합계/포맷 ----------------
def hms_from_seconds(secs: float):
    t = int(secs); return t//3600, (t%3600)//60, t%60
def fmt_hms(secs: float) -> str:
    h,m,s = hms_from_seconds(secs); return f"{h:02d}:{m:02d}:{s:02d}"

def sum_seconds_in_range(uid: int, rs_local: datetime, re_local: datetime) -> float:
    def overlap(a1,a2,b1,b2):
        s=max(a1,b1); e=min(a2,b2); return max(0.0,(e-s).total_seconds())
    total=0.0
    for s_utc,e_utc in records.get(uid,[]):
        s=s_utc.astimezone(); e=e_utc.astimezone()
        total += overlap(s,e,rs_local,re_local)
    if uid in timers:
        s = timers[uid]["start"].astimezone()
        now = datetime.now(timezone.utc).astimezone()
        total += overlap(s,now,rs_local,re_local)
    return total

def sum_seconds_in_single_day(uid:int, day_start_local:datetime)->float:
    return sum_seconds_in_range(uid, day_start_local, day_start_local+timedelta(days=1))

# ---------------- 저장/로드 ----------------
def dt_to_iso(dt:datetime)->str: return dt.astimezone(timezone.utc).isoformat()
def dt_from_iso(s:str)->datetime: return datetime.fromisoformat(s).astimezone(timezone.utc)

def save_records():
    try:
        out={str(uid):[(dt_to_iso(s),dt_to_iso(e)) for s,e in lst] for uid,lst in records.items()}
        with open(RECORDS_JSON,"w",encoding="utf-8") as f: json.dump(out,f,ensure_ascii=False,indent=2)
    except: pass

def load_records():
    try:
        with open(RECORDS_JSON,"r",encoding="utf-8") as f: raw=json.load(f)
        for k,lst in raw.items():
            records[int(k)]=[(dt_from_iso(s),dt_from_iso(e)) for s,e in lst]
    except FileNotFoundError: pass
    except: pass

def save_running():
    try:
        out={str(uid):{"start":dt_to_iso(st["start"]), "mention":st.get("mention"), "avatar":st.get("avatar")} for uid,st in timers.items()}
        with open(RUNNING_JSON,"w",encoding="utf-8") as f: json.dump(out,f,ensure_ascii=False,indent=2)
    except: pass

def load_running_partial():
    try:
        with open(RUNNING_JSON,"r",encoding="utf-8") as f: raw=json.load(f)
        for k,st in raw.items():
            timers[int(k)]={"start":dt_from_iso(st["start"]), "message":None, "mention":st.get("mention"), "avatar":st.get("avatar")}
    except FileNotFoundError: pass
    except: pass

# ---------------- 디스코드 클라이언트 ----------------
intents = discord.Intents.default()
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)

# 진행중/기록
timers: Dict[int, Dict] = {}
records: Dict[int, List[Tuple[datetime, datetime]]] = {}

# ---------------- UI ----------------
def make_embed(mention:str, start_utc:datetime, now_utc:datetime, running:bool, avatar:Optional[str]=None):
    h,m,s = hms_from_seconds((now_utc - start_utc).total_seconds())
    e = discord.Embed(description=f"{mention} 타이머 기록", color=0x2ecc71 if running else 0x95a5a6)
    e.add_field(name="날짜", value=start_utc.astimezone().strftime("%Y-%m-%d"), inline=True)
    e.add_field(name="시간", value=f"{h:02d}:{m:02d}:{s:02d}", inline=True)
    e.add_field(name="상태", value=("진행중" if running else "종료"), inline=True)
    e.set_footer(text="⏱️ 1분 단위 자동 갱신")
    if avatar: e.set_thumbnail(url=avatar)
    return e

async def get_log_channel(guild:discord.Guild):
    if STREAM_LOG_CHANNEL_ID:
        ch = guild.get_channel(STREAM_LOG_CHANNEL_ID)
        if ch: return ch
    if guild.system_channel: return guild.system_channel
    for ch in guild.text_channels:
        if ch.permissions_for(guild.me).send_messages: return ch
    return None

# ---------------- 시작/종료 로직 ----------------
async def start_tracking(member:discord.Member):
    uid=member.id
    if uid in timers: return
    start=datetime.now(timezone.utc)
    mention=member.mention
    avatar=str(member.display_avatar.url)
    timers[uid]={"start":start,"message":None,"mention":mention,"avatar":avatar}
    save_running()
    ch = await get_log_channel(member.guild)
    if ch:
        try:
            msg = await ch.send(embed=make_embed(mention,start,start,True,avatar))
            timers[uid]["message"]=msg
        except: pass

async def end_tracking(member: discord.Member, reason="자동 종료"):
    uid = member.id

    # 1) 현재 상태 확보 (달리던 루프가 이 메시지를 다시 건드리지 못하게 미리 'message=None' 처리)
    state = timers.get(uid)
    msg: Optional[discord.Message] = None
    if state:
        msg = state.get("message")
        # 루프가 list(timers.items()) 스냅샷으로 돌고 있어도 message=None이면 편집을 건너뜀
        state["message"] = None

    # 2) 타이머 테이블에서 제거
    timers.pop(uid, None)

    # 3) 표시용 값들 준비
    start = state["start"] if state else datetime.now(timezone.utc)
    mention = (state.get("mention") if state else None) or member.mention
    avatar = (state.get("avatar") if state else None) or str(member.display_avatar.url)
    now = datetime.now(timezone.utc)

    # 4) 기록(1분 미만 제외)
    dur = (now - start).total_seconds()
    qualify = dur >= 60
    if qualify:
        records.setdefault(uid, []).append((start, now))
        save_records()

    # 5) 시작 때 올린 메시지를 '종료' 상태로 수정 (없으면 새로 1번만 보냄)
    try:
        emb = make_embed(mention, start, now, running=False, avatar_url=avatar)
        if msg:
            await msg.edit(embed=emb)
        else:
            ch = await get_log_channel(member.guild)
            if ch:
                await ch.send(embed=emb)
    except Exception:
        pass

    # 6) 실행중 정보 저장(복구 파일)
    save_running()


# ---------------- 주기 갱신/정리 ----------------
@tasks.loop(seconds=60)
async def update_timer_embeds():
    if not timers: return
    now=datetime.now(timezone.utc)
    for uid,st in list(timers.items()):
        msg=st.get("message")
        if msg:
            try: await msg.edit(embed=make_embed(st["mention"], st["start"], now, True, st["avatar"]))
            except: pass

_last_prune_marker: Optional[str] = None
@tasks.loop(minutes=1)
async def auto_prune_every_tue_4am():
    global _last_prune_marker
    now = datetime.now().astimezone()
    if not (now.weekday()==1 and now.hour==4 and now.minute==0): return
    today_key=now.strftime("%Y-%m-%d")
    if _last_prune_marker==today_key: return
    cutoff=keep_from_monday_after_3_weeks_ago_sunday_local()
    removed=trimmed=0
    for uid,sess in list(records.items()):
        new=[]
        for s_utc,e_utc in sess:
            s=s_utc.astimezone(); e=e_utc.astimezone()
            if e<=cutoff: removed+=1; continue
            if s<cutoff<e:
                new.append((cutoff.astimezone(timezone.utc), e_utc)); trimmed+=1
            else:
                new.append((s_utc,e_utc))
        records[uid]=new
    _last_prune_marker=today_key
    if removed or trimmed: save_records()

# ---------------- 이벤트 ----------------
@bot.event
async def on_ready():
    print(f"✅ 로그인: {bot.user}")

    # 기록/진행중 복구
    load_records()
    load_running_partial()

    # 타이머 갱신/프루닝 루프 시작
    if not update_timer_embeds.is_running():
        update_timer_embeds.start()
    if not auto_prune_every_tue_4am.is_running():
        auto_prune_every_tue_4am.start()

    # 내가 실제로 들어가 있는 길드(서버) 목록을 로그로 보여주기
    print("🛰️ Joined guilds:")
    for g in bot.guilds:
        print(f" - {g.id} | {g.name}")

    # ⚡ GUILD_IDS 무시하고 '현재 들어가 있는 모든 길드'에 슬래시 명령 강제 싱크
    #    (환경변수가 틀려도 작동하게)
    synced_total = 0
    for g in bot.guilds:
        try:
            synced = await bot.tree.sync(guild=discord.Object(id=g.id))
            print(f"✅ Synced {len(synced)} commands to guild {g.id} ({g.name})")
            synced_total += len(synced)
        except Exception as e:
            print(f"❌ Sync failed for {g.id} ({g.name}): {e}")

    # 그래도 하나도 안 잡히면 글로벌 싱크(느릴 수 있지만 마지막 안전망)
    if synced_total == 0:
        try:
            gs = await bot.tree.sync()
            print(f"🪄 Global sync pushed: {len(gs)} commands")
        except Exception as e:
            print(f"❌ Global sync failed: {e}")


@bot.event
async def on_voice_state_update(member:discord.Member, before:discord.VoiceState, after:discord.VoiceState):
    b = getattr(before,"self_stream",False)
    a = getattr(after,"self_stream",False)
    if (not b) and a and after.channel: await start_tracking(member)
    elif (b and not a) or (after.channel is None):
        if member.id in timers: await end_tracking(member, "자동 종료(스트림 종료/퇴장)")

# ---------------- 슬래시 명령 ----------------
@bot.tree.command(name="일일정산", description="오늘 또는 어제의 총 기록을 보여줍니다.")
async def cmd_daily(i:discord.Interaction, 기준:Literal["오늘","어제"]):
    uid=i.user.id
    if 기준=="어제":
        s,e=yesterday_bounds_local(); label=s.strftime("%Y-%m-%d")
    else:
        s,e=today_bounds_local(); label=s.strftime("%Y-%m-%d")
    total=sum_seconds_in_range(uid,s,e)
    ebd=discord.Embed(description=f"{i.user.mention} 일일 정산", color=0x00B894)
    ebd.add_field(name="날짜", value=label, inline=True)
    ebd.add_field(name="총 시간", value=fmt_hms(total), inline=True)
    ebd.set_thumbnail(url=str(i.user.display_avatar.url))
    await i.response.send_message(embed=ebd, ephemeral=True)

@bot.tree.command(name="주간정산", description="이번 주 또는 지난 주의 총 기록을 보여줍니다.")
async def cmd_weekly(i:discord.Interaction, 기준:Literal["이번주","저번주"]):
    uid=i.user.id
    if 기준=="저번주": s,e=last_week_bounds_local_monday_to_sunday()
    else: s,e=week_bounds_local_monday_to_sunday()
    total=sum_seconds_in_range(uid,s,e)
    label=f"{s.strftime('%Y-%m-%d')} ~ {(e - timedelta(days=1)).strftime('%Y-%m-%d')}"
    ebd=discord.Embed(description=f"{i.user.mention} 주간 정산", color=0x0984E3)
    ebd.add_field(name="기간", value=label, inline=False)
    ebd.add_field(name="총 시간", value=fmt_hms(total), inline=True)
    ebd.set_thumbnail(url=str(i.user.display_avatar.url))
    await i.response.send_message(embed=ebd, ephemeral=True)

@bot.tree.command(name="주간일람", description="스터디원 전원의 주간(월~일) 일별 시간 요약을 보여줍니다.")
async def cmd_roster(i: discord.Interaction, 기준: Literal["이번주", "저번주"]):
    # 디스코드에 "생각중…" 표시 먼저 띄우기 (타임아웃 방지)
    await i.response.defer(ephemeral=False)

    try:
        # 주간 범위 계산
        if 기준 == "저번주":
            week_start, week_end = last_week_bounds_local_monday_to_sunday()
        else:
            week_start, week_end = week_bounds_local_monday_to_sunday()
        days = [week_start + timedelta(days=k) for k in range(7)]

        # 후보 사용자 모으기 (기록 있거나 진행중)
        candidate_uids = set(records.keys())
        for uid, st in timers.items():
            if st["start"].astimezone() < week_end:
                candidate_uids.add(uid)

        guild = i.guild

        def name_for(uid: int) -> str:
            m = guild.get_member(uid) if guild else None
            return m.display_name if m else f"User {uid}"

        per_user = []
        for uid in candidate_uids:
            weekly_total = sum_seconds_in_range(uid, days[0], week_end)
            if weekly_total <= 0:
                continue
            day_rows = []
            for d in days:
                secs = sum_seconds_in_single_day(uid, d)
                day_rows.append((d, secs))
            per_user.append((uid, name_for(uid), weekly_total, day_rows))

        # 주간 합계 내림차순
        per_user.sort(key=lambda x: x[2], reverse=True)

        if not per_user:
            await i.followup.send("이번 주에는 기록이 없어요.")
            return

        # 임베드 생성: 제목 대신 본문 첫줄에 @멘션 + 썸네일(멤버 있을 때만)
        embeds: List[discord.Embed] = []
        for uid, uname, weekly_total, day_rows in per_user:
            mention = f"<@{uid}>"
            emb = discord.Embed(
                description=f"{mention} 님의 주간 기록",
                color=0x6C5CE7
            )

            member = guild.get_member(uid) if guild else None
            if member:
                emb.set_thumbnail(url=str(member.display_avatar.url))

            for d, secs in day_rows:
                label = format_md_wd(d.astimezone())
                emb.add_field(name=label, value=fmt_hms(secs), inline=True)

            emb.add_field(name="주간 합계", value=fmt_hms(weekly_total), inline=False)
            embeds.append(emb)

        # 10개씩 나눠 전송
        for k in range(0, len(embeds), 10):
            await i.followup.send(embeds=embeds[k:k+10])

    except Exception as e:
        # 문제 생기면 이유를 바로 보여주기
        print(f"❌ /주간일람 에러: {e}")
        await i.followup.send(f"❌ 주간일람 처리 중 에러가 났어요: {e}", ephemeral=True)


@bot.tree.command(name="도움말", description="스터디봇 명령어 안내")
async def cmd_help(i:discord.Interaction):
    e=discord.Embed(title="📖 도움말", color=0xFFD166)
    e.add_field(name="자동 측정", value="화면공유(Go Live) 시작→자동 기록, 종료→자동 저장. 1분 미만 제외.", inline=False)
    e.add_field(name="/일일정산", value="오늘/어제 개인 총시간", inline=False)
    e.add_field(name="/주간정산", value="이번주/저번주 개인 총시간", inline=False)
    e.add_field(name="/주간일람", value="전체 멤버 월~일 일별 시간표", inline=False)
    await i.response.send_message(embed=e, ephemeral=True)

# ---------------- 실행 ----------------
if not DISCORD_TOKEN:
    raise RuntimeError("토큰 부족! 방장에게 문의해주세요.")
bot.run(DISCORD_TOKEN)