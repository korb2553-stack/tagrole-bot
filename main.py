import os
import json
import time
import random
import asyncio
import discord
from discord import app_commands
from discord.ext import tasks

TOKEN = os.getenv("DISCORD_TOKEN")  # ใส่ในช่อง Environment ของโฮสต์ ห้ามเขียนทับในโค้ด
CONFIG_FILE = "tagrole_config.json"
STATE_FILE = "tagrole_state.json"
MSG_FILE = "tagrole_msgs.json"

DEFAULT_CONFIG = {
    "role_id": None,
    "channel_id": None,
    "role_desc": "ยศนี้ใช้สำหรับคนที่ใส่แท็กเซิร์ฟเวอร์",
    "give_title": "🏷️ ได้รับยศจากแท็กเซิร์ฟเวอร์",
    "give_msg": (
        "{user} ใส่แท็ก **{tag}** ของ {server} แล้ว!\n\n"
        "🎖️ ยศที่ได้รับ: {role}\n"
        "📌 ยศนี้ทำอะไรได้: {desc}"
    ),
    "remove_title": "🏷️ คืนยศแล้ว",
    "remove_msg": "{user} ไม่ได้ใช้แท็ก {server} แล้ว คืนยศ {role}",
    "color": "#9b59b6",
}


def load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(default, dict):
            return {**default, **data}
        return data
    except Exception:
        return default.copy() if isinstance(default, dict) else list(default)


def save(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


config = load(CONFIG_FILE, DEFAULT_CONFIG)
granted = set(load(STATE_FILE, []))  # id คนที่บอทเป็นคนให้ยศ (กันลบยศที่แอดมินแจกเอง)


msgs = load(MSG_FILE, {})  # id คน -> [ห้อง, ข้อความ] ของข้อความคืนยศล่าสุด


def save_state():
    save(STATE_FILE, sorted(granted))


intents = discord.Intents.default()
intents.members = True  # ต้องเปิด Server Members Intent ใน Developer Portal

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)


def has_tag(member: discord.Member, guild: discord.Guild) -> bool:
    pg = getattr(member, "primary_guild", None)
    if pg is None:
        return False
    return bool(getattr(pg, "identity_enabled", False)) and getattr(pg, "id", None) == guild.id


def fmt(template: str, member: discord.Member, role: discord.Role) -> str:
    pg = getattr(member, "primary_guild", None)
    tag = getattr(pg, "tag", None) or member.guild.name
    return (
        template.replace("{user}", member.mention)
        .replace("{role}", role.mention)
        .replace("{role_name}", role.name)
        .replace("{desc}", config["role_desc"])
        .replace("{server}", member.guild.name)
        .replace("{tag}", tag)
    )


def make_embed(title: str, text: str, member: discord.Member) -> discord.Embed:
    try:
        color = discord.Color.from_str(config["color"])
    except Exception:
        color = discord.Color.purple()
    e = discord.Embed(title=title, description=text, color=color)
    e.set_thumbnail(url=member.display_avatar.url)
    return e


async def announce(guild: discord.Guild, member: discord.Member, title: str, text: str):
    if not config["channel_id"]:
        return None
    ch = guild.get_channel(config["channel_id"])
    if ch is None:
        return None
    # กันซ้ำข้ามอินสแตนซ์: ถ้าบอทเพิ่งส่งข้อความหัวข้อเดียวกันถึงคนนี้ใน 30 วินาที ไม่ส่งอีก
    try:
        async for m in ch.history(limit=10):
            if (
                m.author.id == bot.user.id
                and m.embeds
                and m.embeds[0].title == title
                and member.mention in (m.content or "")
                and (discord.utils.utcnow() - m.created_at).total_seconds() < 30
            ):
                return None
    except discord.HTTPException:
        pass
    try:
        return await ch.send(
            content=member.mention,
            embed=make_embed(title, text, member),
            allowed_mentions=discord.AllowedMentions(users=[member], roles=False),
        )
    except discord.HTTPException:
        return None


async def delete_old(guild: discord.Guild, ref):
    ch = guild.get_channel(ref[0])
    if ch is None:
        return
    try:
        m = await ch.fetch_message(ref[1])
        await m.delete()
    except discord.HTTPException:
        pass


locks = {}
last_action = {}


async def check_member(member: discord.Member):
    # กันอีเวนต์หลายตัวเรียกพร้อมกันจนส่งข้อความซ้ำ
    lock = locks.setdefault(member.id, asyncio.Lock())
    async with lock:
        await _check_member(member)


async def _check_member(member: discord.Member):
    if member.bot or not config["role_id"]:
        return
    if time.time() - last_action.get(member.id, 0) < 15:
        return
    guild = member.guild
    role = guild.get_role(config["role_id"])
    if role is None:
        return

    wants = has_tag(member, guild)
    owns = role in member.roles
    needs_action = (wants and not owns) or (not wants and owns and member.id in granted)

    if needs_action:
        # หน่วงสุ่มสั้น ๆ แล้วดึงข้อมูลสดอีกรอบ กันกรณีมีบอทหลายตัวทำงานพร้อมกัน
        await asyncio.sleep(random.uniform(0, 1.5))
        try:
            member = await guild.fetch_member(member.id)
        except discord.HTTPException:
            return
        wants = has_tag(member, guild)
        owns = role in member.roles

    if wants and not owns:
        try:
            await member.add_roles(role, reason="ใช้แท็กเซิร์ฟเวอร์")
        except discord.HTTPException:
            return
        granted.add(member.id)
        last_action[member.id] = time.time()
        save_state()
        # ลบข้อความ "คืนยศ" เก่าของคนนี้ออก
        old = msgs.pop(str(member.id), None)
        if old:
            save(MSG_FILE, msgs)
            await delete_old(guild, old)
        await announce(guild, member, config["give_title"], fmt(config["give_msg"], member, role))

    elif wants and owns:
        if member.id not in granted:  # มียศอยู่แล้วตอนบอทเริ่ม ไม่ต้องประกาศซ้ำ
            granted.add(member.id)
            save_state()

    elif not wants and owns and member.id in granted:
        try:
            await member.remove_roles(role, reason="ถอดแท็กเซิร์ฟเวอร์")
        except discord.HTTPException:
            return
        granted.discard(member.id)
        last_action[member.id] = time.time()
        save_state()
        sent = await announce(guild, member, config["remove_title"], fmt(config["remove_msg"], member, role))
        if sent:
            msgs[str(member.id)] = [sent.channel.id, sent.id]
            save(MSG_FILE, msgs)


@bot.event
async def on_ready():
    await tree.sync()
    if not sweep.is_running():
        sweep.start()
    print(f"ออนไลน์แล้ว: {bot.user}")


@bot.event
async def on_member_update(before, after):
    await check_member(after)


@bot.event
async def on_user_update(before, after):
    for g in bot.guilds:
        m = g.get_member(after.id)
        if m:
            await check_member(m)


@tasks.loop(seconds=60)
async def sweep():
    # เช็กซ้ำทุก 1 นาที กันกรณี Discord ไม่ส่งอีเวนต์เปลี่ยนแท็ก
    for g in bot.guilds:
        for m in g.members:
            await check_member(m)


# ---------------- คำสั่งตั้งค่า (แอดมินเท่านั้น) ----------------
group = app_commands.Group(
    name="tagrole",
    description="ตั้งค่าระบบยศจากแท็กเซิร์ฟเวอร์",
    default_permissions=discord.Permissions(administrator=True),
    guild_only=True,
)


@group.command(name="role", description="ตั้งยศที่จะแจกเมื่อใส่แท็ก")
@app_commands.describe(role="ยศที่ต้องการแจก", desc="ยศนี้ทำอะไรได้ (ใช้แสดงในข้อความ)")
async def set_role(i: discord.Interaction, role: discord.Role, desc: str = None):
    if role >= i.guild.me.top_role:
        return await i.response.send_message(
            "❌ ยศนี้สูงกว่ายศบอท ลากยศบอทให้อยู่เหนือยศนี้ก่อน", ephemeral=True
        )
    config["role_id"] = role.id
    if desc:
        config["role_desc"] = desc
    save(CONFIG_FILE, config)
    await i.response.send_message(f"✅ ตั้งยศเป็น {role.mention} แล้ว", ephemeral=True)


@group.command(name="channel", description="ตั้งห้องที่บอทจะส่งประกาศ")
async def set_channel(i: discord.Interaction, channel: discord.TextChannel):
    config["channel_id"] = channel.id
    save(CONFIG_FILE, config)
    await i.response.send_message(f"✅ ส่งประกาศที่ {channel.mention}", ephemeral=True)


@group.command(name="desc", description="แก้คำอธิบายว่ายศนี้ทำอะไรได้")
async def set_desc(i: discord.Interaction, text: str):
    config["role_desc"] = text
    save(CONFIG_FILE, config)
    await i.response.send_message("✅ แก้คำอธิบายยศแล้ว", ephemeral=True)


@group.command(name="message", description="แก้ข้อความตอนได้ยศ / ตอนคืนยศ")
@app_commands.describe(
    kind="ข้อความตอนไหน",
    text="ใช้ได้: {user} {role} {role_name} {desc} {server} {tag} ขึ้นบรรทัดใหม่ใช้ \\n",
    title="หัวข้อ embed (ไม่ใส่ก็ได้)",
)
@app_commands.choices(
    kind=[
        app_commands.Choice(name="ตอนได้ยศ", value="give"),
        app_commands.Choice(name="ตอนคืนยศ", value="remove"),
    ]
)
async def set_message(i: discord.Interaction, kind: app_commands.Choice[str], text: str, title: str = None):
    config[f"{kind.value}_msg"] = text.replace("\\n", "\n")
    if title:
        config[f"{kind.value}_title"] = title
    save(CONFIG_FILE, config)
    await i.response.send_message("✅ แก้ข้อความแล้ว", ephemeral=True)


@group.command(name="color", description="เปลี่ยนสี embed เช่น #ff66aa")
async def set_color(i: discord.Interaction, hex_color: str):
    try:
        discord.Color.from_str(hex_color)
    except Exception:
        return await i.response.send_message("❌ สีไม่ถูกต้อง ตัวอย่าง #9b59b6", ephemeral=True)
    config["color"] = hex_color
    save(CONFIG_FILE, config)
    await i.response.send_message("✅ เปลี่ยนสีแล้ว", ephemeral=True)


@group.command(name="preview", description="ดูตัวอย่างข้อความ")
async def preview(i: discord.Interaction):
    role = i.guild.get_role(config["role_id"]) if config["role_id"] else None
    if role is None:
        return await i.response.send_message("❌ ยังไม่ได้ตั้งยศ ใช้ /tagrole role ก่อน", ephemeral=True)
    m = i.user
    await i.response.send_message(
        embeds=[
            make_embed(config["give_title"], fmt(config["give_msg"], m, role), m),
            make_embed(config["remove_title"], fmt(config["remove_msg"], m, role), m),
        ],
        ephemeral=True,
    )


tree.add_command(group)

if __name__ == "__main__":
    bot.run(TOKEN)
