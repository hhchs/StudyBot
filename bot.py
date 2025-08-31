# [test] bot.py — /출석(실시간), 종료 요약, 프로필 썸네일, /일일정산, /주간정산
import os
import discord
from discord.ext import commands, tasks
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from typing import Dict, List, Tuple, Optional
from typing import Literal

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_IDS = [int(x) for x in os.getenv("GUILD_IDS","").split(",") if x.strip()]




def yesterday_bounds_local():
    now_local = datetime.now().astimezone()
    today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    y_start = today_start - timedelta(days=1)
    y_end = today_start
    return y_start, y_end

def last_week_bounds_local_monday_to_sunday():
    # 이번 주 월요일
    now_local = datetime.now().astimezone()
    this_monday = (now_local - timedelta(days=now_local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    # 지난 주 월요일 ~ 이번 주 월요일
    last_monday = this_monday - timedelta(days=7)
    return last_monday, this_monday



intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)  # prefix는 사용하지 않음

# 진행중 타이머: { user_id: {"start": datetime_utc, "message": Message, "view": View, "mention": str, "avatar": str} }
timers: Dict[int, Dict] = {}

# 완료된 세션 기록: { user_id: [ (start_utc: datetime, end_utc: datetime) , ... ] }
records: Dict[int, List[Tuple[datetime, datetime]]] = {}

# ---------- 유틸 ----------

def hms_from_seconds(secs: float) -> Tuple[int, int, int]:
    t = int(secs)
    return t // 3600, (t % 3600) // 60, t % 60

def fmt_hms(secs: float) -> str:
    h, m, s = hms_from_seconds(secs)
    return f"{h:02d}:{m:02d}:{s:02d}"

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

def sum_seconds_in_range(user_id: int, range_start_local: datetime, range_end_local: datetime) -> float:
    """
    로컬시간 기준 [range_start_local, range_end_local) 구간의 총 기록(초)을 계산.
    - 완료 세션: records[user_id]
    - 진행 중 타이머가 범위와 겹치면 지금까지 포함
    """
    total = 0.0
    # 범위를 로컬시간에서 UTC로 직접 바꾸는 대신, 세션을 로컬로 변환해서 겹치는 구간 계산
    # 구간은 [start, end)
    def overlap_seconds(a1: datetime, a2: datetime, b1: datetime, b2: datetime) -> float:
        start = max(a1, b1)
        end = min(a2, b2)
        return max(0.0, (end - start).total_seconds())

    # 완료 세션 합산
    for (s_utc, e_utc) in records.get(user_id, []):
        s_local = s_utc.astimezone()
        e_local = e_utc.astimezone()
        total += overlap_seconds(s_local, e_local, range_start_local, range_end_local)

    # 진행 중이면 지금까지 포함
    if user_id in timers:
        s_utc = timers[user_id]["start"]
        s_local = s_utc.astimezone()
        now_local = datetime.now(timezone.utc).astimezone()
        total += overlap_seconds(s_local, now_local, range_start_local, range_end_local)

    return total

def today_bounds_local() -> Tuple[datetime, datetime]:
    now_local = datetime.now().astimezone()
    start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start, end

def week_bounds_local_monday_to_sunday() -> Tuple[datetime, datetime]:
    now_local = datetime.now().astimezone()
    # 월요일=0 ... 일요일=6
    monday = (now_local - timedelta(days=now_local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    next_monday = monday + timedelta(days=7)
    return monday, next_monday

def keep_from_monday_after_3_weeks_ago_sunday_local() -> datetime:
    """
    지금(로컬) 기준 3주 전의 '그 주' 일요일 다음날 00:00(=월요일 00:00)을 반환.
    예) 8/19(화) 04:00에 호출 → 3주 전은 7/29(화), 그 주의 일요일은 8/3,
       반환값은 8/4 00:00 (이 시각부터의 기록만 남김)
    """
    now_local = datetime.now().astimezone()
    three_weeks_ago = now_local - timedelta(weeks=3)

    # three_weeks_ago 가 속한 주의 '월요일 00:00'
    monday_of_that_week = (three_weeks_ago - timedelta(days=three_weeks_ago.weekday())) \
        .replace(hour=0, minute=0, second=0, microsecond=0)

    # 그 주의 일요일 다음날(=다음 주 월요일 00:00) == 우리가 남길 시작점
    keep_from_monday = monday_of_that_week + timedelta(days=7)
    return keep_from_monday

def prune_old_records(cutoff_local: datetime) -> Tuple[int, int]:
    """
    cutoff_local(로컬 시간) 이전 데이터를 records에서 정리.
    - 세션의 종료시각(e_local)이 cutoff_local 이하이면 '완전 삭제' (removed += 1)
    - 세션이 경계를 걸치면 시작시각을 cutoff_local로 '잘라서 보존' (trimmed += 1)
    반환: (removed, trimmed)
    """
    removed = 0
    trimmed = 0

    for uid, sess_list in list(records.items()):
        new_list: List[Tuple[datetime, datetime]] = []
        for s_utc, e_utc in sess_list:
            s_local = s_utc.astimezone()
            e_local = e_utc.astimezone()

            # 1) 완전히 과거면 삭제 (끝난 시간이 컷오프 '이전 또는 같음')
            if e_local <= cutoff_local:
                removed += 1
                continue

            # 2) 경계를 걸치면 시작을 컷오프로 잘라서 보존
            if s_local < cutoff_local < e_local:
                cutoff_utc = cutoff_local.astimezone(timezone.utc)
                new_list.append((cutoff_utc, e_utc))
                trimmed += 1
            else:
                # 3) 컷오프 이후만으로 구성된 세션 → 그대로 보존
                new_list.append((s_utc, e_utc))

        records[uid] = new_list

    return removed, trimmed



# ---------- 뷰/버튼 ----------

class StopView(discord.ui.View):
    def __init__(self, starter_id: int):
        super().__init__(timeout=None)
        self.starter_id = starter_id

    @discord.ui.button(label="⏹️ 측정 종료", style=discord.ButtonStyle.danger)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.starter_id:
            await interaction.response.send_message("본인 기록만 종료 가능합니다. **/출석**을 입력해 기록을 시작해주세요.", ephemeral=True)
            return

        state = timers.pop(self.starter_id, None)
        if not state:
            await interaction.response.send_message("진행 중인 기록이 없습니다. **/출석**을 입력해 기록을 시작해주세요.", ephemeral=True)
            return

        start_at: datetime = state["start"]
        msg: discord.Message = state["message"]
        mention: str = state["mention"]
        avatar: str = state["avatar"]
        now = datetime.now(timezone.utc)

        # ✅ 세션 기록 저장
        records.setdefault(interaction.user.id, []).append((start_at, now))

        # 버튼 비활성화
        for child in self.children:
            child.disabled = True

        # 채널 메시지 → 종료 상태로 갱신
        await msg.edit(embed=make_embed(mention, start_at, now, running=False, avatar_url=avatar), view=self)

        # 개인 요약 임베드 전송
        date_str = start_at.astimezone().strftime("%Y-%m-%d")
        result = discord.Embed(description=f"{interaction.user.mention} 타이머 기록", color=0x5865F2)
        result.add_field(name="날짜", value=date_str, inline=True)
        result.add_field(name="시간", value=f"{fmt_hms((now - start_at).total_seconds())}", inline=True)
        result.set_thumbnail(url=avatar)

        await interaction.response.send_message(embed=result, ephemeral=True)

# ---------- 이벤트/루프 ----------

@bot.event
async def on_ready():
    print(f"✅ 로그인: {bot.user}")

    # 주기 작업 시작
    if not update_timer_embeds.is_running():
        update_timer_embeds.start()
    if not auto_prune_every_tue_4am.is_running():
        auto_prune_every_tue_4am.start()

    # 전역으로 정의한 명령들을 각 길드 트리로 복사 → 해당 길드 트리를 동기화
    for gid in GUILD_IDS:
        guild_obj = discord.Object(id=int(gid))
        try:
            bot.tree.clear_commands(guild=guild_obj)      # 이전 캐시를 비우고
            bot.tree.copy_global_to(guild=guild_obj)      # 전역 명령을 길드로 복사
            synced = await bot.tree.sync(guild=guild_obj) # ← 이 줄은 'await'가 맞아야 함
            print(f"✅ Synced {len(synced)} commands to guild {gid}")
        except Exception as e:
            print(f"❌ Sync failed for {gid}: {e}")







@tasks.loop(seconds=60)
async def update_timer_embeds():
    """모든 진행중 타이머 임베드를 60초마다 갱신"""
    if not timers:
        return
    now = datetime.now(timezone.utc)
    for uid, state in list(timers.items()):
        start_at: datetime = state["start"]
        msg: discord.Message = state["message"]
        view: discord.ui.View = state["view"]
        mention: str = state["mention"]
        avatar: str = state["avatar"]
        try:
            await msg.edit(embed=make_embed(mention, start_at, now, running=True, avatar_url=avatar), view=view)
        except Exception:
            pass

# ---------- 슬래시 명령 ----------

@bot.tree.command(name="출석", description="스터디 기록을 시작합니다.")
async def slash_checkin(interaction: discord.Interaction):
    uid = interaction.user.id
    if uid in timers:
        await interaction.response.send_message("이미 기록이 진행 중입니다. 메시지의 **⏹️ 측정 종료** 버튼으로 이전 기록을 종료해주세요.", ephemeral=True)
        return

    start_at = datetime.now(timezone.utc)
    view = StopView(starter_id=uid)
    mention = interaction.user.mention
    avatar = str(interaction.user.display_avatar.url)

    emb = make_embed(mention, start_at, start_at, running=True, avatar_url=avatar)
    await interaction.response.defer()
    msg = await interaction.channel.send(embed=emb, view=view)

    timers[uid] = {"start": start_at, "message": msg, "view": view, "mention": mention, "avatar": avatar}


@bot.tree.command(name="일일정산", description="오늘 또는 어제의 총 기록을 보여줍니다.")
async def slash_daily_settle(
    interaction: discord.Interaction,
    기준: Literal["오늘", "어제"]  # ← 이 값들이 자동으로 옵션으로 뜸
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
    기준: Literal["이번주", "저번주"]  # ← 이것도 자동으로 옵션 생성
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



@bot.tree.command(name="도움말", description="스터디봇의 명령어 목록을 보여줍니다.")
async def help_command(interaction: discord.Interaction):
    emb = discord.Embed(
        title="📖 도움말",
        color=0xFFD166
    )
    emb.add_field(
        name="/출석",
        value="타이머가 시작됩니다. 버튼으로 기록을 종료할 수 있습니다.",
        inline=False
    )
    emb.add_field(
        name="/일일정산",
        value="오늘 또는 어제의 총 기록을 보여줍니다. 진행 중인 타이머도 포함됩니다.",
        inline=False
    )
    emb.add_field(
        name="/주간정산",
        value="이번 주 또는 지난 주의 총 기록을 보여줍니다. 진행 중인 타이머도 포함됩니다.",
        inline=False
    )

    await interaction.response.send_message(embed=emb, ephemeral=True)


_last_prune_marker: str | None = None  # 같은 날 중복 실행 방지용 메모리 마커

@tasks.loop(minutes=1)
async def auto_prune_every_tue_4am():
    """
    매 분 체크해서, 로컬 기준 화요일 04:00이 되면 3주 전 일요일 이전 기록을 정리.
    (정확히는 월요일 00:00을 컷오프로 사용)
    """
    global _last_prune_marker
    now_local = datetime.now().astimezone()

    # 화요일(월=0, 화=1), 04:00, 정확히 분=0일 때만 실행
    if not (now_local.weekday() == 1 and now_local.hour == 4 and now_local.minute == 0):
        return

    # 같은 날에 여러 번 돌지 않도록 날짜 마커로 가드
    today_key = now_local.strftime("%Y-%m-%d")
    if _last_prune_marker == today_key:
        return

    cutoff = keep_from_monday_after_3_weeks_ago_sunday_local()  # 예: 8/4 00:00
    removed, trimmed = prune_old_records(cutoff)  # 누나가 이전에 붙인 함수 재사용

    _last_prune_marker = today_key




# ---------- 실행 ----------

if not DISCORD_TOKEN:
    raise RuntimeError("토큰 부족! 방장에게 문의해주세요.")
bot.run(DISCORD_TOKEN)