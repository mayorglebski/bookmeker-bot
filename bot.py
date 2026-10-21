import os
import re
from datetime import datetime, timezone

import discord
from discord import app_commands
from dotenv import load_dotenv

import database as db

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
ADMIN_ROLE = os.getenv("ADMIN_ROLE", "Букмекер")
PLAYER_ROLE = os.getenv("PLAYER_ROLE", "Игрок")

LOG_CHANNEL_KEY = "log_channel_id"

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


# ============================================================
# ХЕЛПЕРЫ
# ============================================================
def is_admin(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    return any(r.name == ADMIN_ROLE for r in interaction.user.roles)


def get_player_role(guild: discord.Guild):
    return discord.utils.get(guild.roles, name=PLAYER_ROLE)


def get_dota_rank(balance: int) -> str:
    if balance < 500:
        return "🛡 Рекрут"
    if balance < 1500:
        return "⚔️ Страж"
    if balance < 4000:
        return "🏆 Герой"
    if balance < 10000:
        return "👑 Властелин"
    return "🌟 Титан (Immortal)"


def format_match(m, show_heroes=True):
    emoji_status = {"open": "🟢 открыт", "closed": "🟡 ставки закрыты", "finished": "⚪ завершён"}
    lines = [
        f"**Матч #{m['id']}** — {emoji_status.get(m['status'], m['status'])}",
        f"🅰️ **{m['team1']}** — коэф. **{m['coef1']:.2f}**",
        f"🅱️ **{m['team2']}** — коэф. **{m['coef2']:.2f}**",
    ]
    if show_heroes:
        lines.append(f"🦸 Герои {m['team1']}: `{m['heroes1'] or '—'}`")
        lines.append(f"🦸 Герои {m['team2']}: `{m['heroes2'] or '—'}`")
    if m["status"] == "finished" and m["winner"]:
        winner_name = m["team1"] if m["winner"] == 1 else m["team2"]
        lines.append(f"🏆 Победила: **{winner_name}**")
    return "\n".join(lines)


async def log_to_channel(embed: discord.Embed):
    cid = db.get_setting(LOG_CHANNEL_KEY)
    if not cid:
        return
    try:
        ch = client.get_channel(int(cid))
    except (TypeError, ValueError):
        return
    if ch:
        try:
            await ch.send(embed=embed)
        except discord.Forbidden:
            pass


# ============================================================
# МОДАЛКА СТАВКИ
# ============================================================
class BetModal(discord.ui.Modal, title="Сделать ставку"):
    def __init__(self, match_id: int, team: int, team_name: str, coef: float):
        super().__init__()
        self.match_id = match_id
        self.team = team
        self.coef = coef
        self.team_name = team_name
        self.amount_input = discord.ui.TextInput(
            label=f"Сумма на {team_name} (коэф {coef:.2f})",
            placeholder="Например: 100",
            min_length=1,
            max_length=9,
        )
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = int(self.amount_input.value)
        except ValueError:
            await interaction.response.send_message("❌ Сумма должна быть числом.", ephemeral=True)
            return
        coef, err = db.place_bet(interaction.user.id, self.match_id, self.team, amount)
        if err:
            await interaction.response.send_message(f"❌ {err}", ephemeral=True)
            return
        new_bal = db.get_balance(interaction.user.id)
        possible = int(amount * coef)
        await interaction.response.send_message(
            f"✅ Ставка принята!\n"
            f"Матч #{self.match_id}: **{self.team_name}**\n"
            f"Сумма: **{amount}**, коэф: **{coef:.2f}**\n"
            f"Потенциальный выигрыш: **{possible}**\n"
            f"Остаток: **{new_bal}**",
            ephemeral=True
        )
        emb = discord.Embed(title="🎲 Новая ставка", color=discord.Color.green())
        emb.add_field(name="Игрок", value=interaction.user.mention, inline=True)
        emb.add_field(name="Матч", value=f"#{self.match_id}", inline=True)
        emb.add_field(name="На команду", value=self.team_name, inline=True)
        emb.add_field(name="Сумма", value=str(amount), inline=True)
        emb.add_field(name="Коэф.", value=f"{coef:.2f}", inline=True)
        emb.add_field(name="Возможный выигрыш", value=str(possible), inline=True)
        await log_to_channel(emb)


# ============================================================
# КНОПКИ СТАВОК ДЛЯ ПУБЛИЧНОГО АНОНСА
# ============================================================
class BetView(discord.ui.View):
    def __init__(self, match):
        super().__init__(timeout=None)
        b1 = discord.ui.Button(
            label=f"{match['team1']}  ×{match['coef1']:.2f}",
            style=discord.ButtonStyle.primary,
            custom_id=f"bet:{match['id']}:1",
        )
        b1.callback = self._make(match, 1)
        self.add_item(b1)
        b2 = discord.ui.Button(
            label=f"{match['team2']}  ×{match['coef2']:.2f}",
            style=discord.ButtonStyle.danger,
            custom_id=f"bet:{match['id']}:2",
        )
        b2.callback = self._make(match, 2)
        self.add_item(b2)

    def _make(self, match, team):
        async def cb(interaction: discord.Interaction):
            m = db.get_match(match["id"])
            if not m or m["status"] != "open":
                await interaction.response.send_message(
                    "❌ Приём ставок на этот матч закрыт.", ephemeral=True
                )
                return
            team_name = m["team1"] if team == 1 else m["team2"]
            coef = m["coef1"] if team == 1 else m["coef2"]
            await interaction.response.send_modal(BetModal(m["id"], team, team_name, coef))
        return cb


# ============================================================
# ИГРОК: ГЛАВНОЕ МЕНЮ
# ============================================================
class PlayerMenuView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="💰 Баланс", style=discord.ButtonStyle.primary)
    async def btn_balance(self, interaction: discord.Interaction, button: discord.ui.Button):
        u = db.get_user(interaction.user.id)
        rank = get_dota_rank(u["balance"])
        w, l = u["wins"], u["losses"]
        total = w + l
        wr = f"{w / total * 100:.0f}%" if total else "—"
        try:
            last = datetime.fromisoformat(u["last_daily"]).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            last = datetime(2000, 1, 1, tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - last).total_seconds()
        if elapsed >= db.DAILY_COOLDOWN_HOURS * 3600:
            daily = "✅ готов"
        else:
            rem = db.DAILY_COOLDOWN_HOURS * 3600 - elapsed
            daily = f"⏳ {int(rem // 3600)}ч {int((rem % 3600) // 60)}мин"
        emb = discord.Embed(title=f"💼 {interaction.user.display_name}", color=discord.Color.gold())
        emb.add_field(name="Баланс", value=f"**{u['balance']}** фантиков", inline=True)
        emb.add_field(name="Ранг", value=rank, inline=True)
        emb.add_field(name="Daily", value=daily, inline=True)
        emb.add_field(name="Побед", value=str(w), inline=True)
        emb.add_field(name="Поражений", value=str(l), inline=True)
        emb.add_field(name="Winrate", value=wr, inline=True)
        emb.set_footer(text="Используй /play для возврата")
        await interaction.response.send_message(embed=emb, ephemeral=True)

    @discord.ui.button(label="🎁 Daily", style=discord.ButtonStyle.success)
    async def btn_daily(self, interaction: discord.Interaction, button: discord.ui.Button):
        ok, msg, _ = db.claim_daily(interaction.user.id)
        color = discord.Color.green() if ok else discord.Color.orange()
        emb = discord.Embed(description=msg, color=color)
        await interaction.response.send_message(embed=emb, ephemeral=True)

    @discord.ui.button(label="🎲 Матчи", style=discord.ButtonStyle.primary)
    async def btn_matches(self, interaction: discord.Interaction, button: discord.ui.Button):
        ms = db.get_open_matches()
        if not ms:
            await interaction.response.send_message("Сейчас нет открытых матчей.", ephemeral=True)
            return
        view = MatchesSelectView(ms)
        emb = discord.Embed(
            title="🎲 Открытые матчи",
            description="Выбери матч из списка ниже, чтобы посмотреть детали и сделать ставку.",
            color=discord.Color.blue(),
        )
        await interaction.response.send_message(embed=emb, view=view, ephemeral=True)

    @discord.ui.button(label="📋 Мои ставки", style=discord.ButtonStyle.secondary)
    async def btn_mybets(self, interaction: discord.Interaction, button: discord.ui.Button):
        bets = db.get_user_bets(interaction.user.id)
        if not bets:
            await interaction.response.send_message("У тебя нет активных ставок.", ephemeral=True)
            return
        lines = []
        for b in bets:
            team_name = b["team1"] if b["team"] == 1 else b["team2"]
            status = "🟢 в игре" if b["mstatus"] == "open" else "🟡 ставки закрыты"
            lines.append(f"#{b['match_id']} → **{team_name}** | {b['amount']} × {b['coef']:.2f} | {status}")
        emb = discord.Embed(title="📋 Твои активные ставки", description="\n".join(lines), color=discord.Color.blue())
        await interaction.response.send_message(embed=emb, ephemeral=True)

    @discord.ui.button(label="📜 История", style=discord.ButtonStyle.secondary)
    async def btn_history(self, interaction: discord.Interaction, button: discord.ui.Button):
        bets = db.get_history(interaction.user.id)
        if not bets:
            await interaction.response.send_message("Ставок не найдено.", ephemeral=True)
            return
        lines = []
        for b in bets[:20]:
            team_name = b["team1"] if b["team"] == 1 else b["team2"]
            if b["mstatus"] == "finished":
                if b["winner"] and b["team"] == b["winner"]:
                    res = f"✅ +{int(b['amount'] * b['coef'])}"
                else:
                    res = f"❌ -{b['amount']}"
            else:
                res = "⏳"
            lines.append(f"#{b['match_id']} · **{team_name}** · {b['amount']} × {b['coef']:.2f} → {res}")
        emb = discord.Embed(
            title=f"📜 История · {interaction.user.display_name}",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        await interaction.response.send_message(embed=emb, ephemeral=True)


# ============================================================
# ИГРОК: ВЫБОР МАТЧА ДЛЯ ПРОСМОТРА / СТАВКИ
# ============================================================
class MatchesSelectView(discord.ui.View):
    def __init__(self, matches):
        super().__init__(timeout=180)
        self.matches = {m["id"]: m for m in matches}
        options = [
            discord.SelectOption(
                label=f"#{m['id']} · {m['team1']} vs {m['team2']}"[:100],
                value=str(m["id"]),
                description=f"{m['coef1']:.2f} / {m['coef2']:.2f}"[:100],
            )
            for m in matches[:25]
        ]
        self.select = discord.ui.Select(placeholder="Выбери матч…", options=options)
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction):
        mid = int(self.select.values[0])
        m = db.get_match(mid)
        if not m:
            await interaction.response.send_message("❌ Матч не найден.", ephemeral=True)
            return
        emb = discord.Embed(
            title=f"🎲 Матч #{m['id']}",
            description=format_match(m),
            color=discord.Color.blue(),
        )
        view = BetView(m) if m["status"] == "open" else None
        await interaction.response.send_message(embed=emb, view=view, ephemeral=True)


# ============================================================
# БУКМЕКЕР: ГЛАВНОЕ МЕНЮ
# ============================================================
class AdminMenuView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="➕ Новый матч", style=discord.ButtonStyle.success, row=0)
    async def btn_newmatch(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(NewMatchModal())

    @discord.ui.button(label="🎮 Управление матчами", style=discord.ButtonStyle.primary, row=0)
    async def btn_manage(self, interaction: discord.Interaction, button: discord.ui.Button):
        ms = db.get_matches_for_manage()
        if not ms:
            await interaction.response.send_message(
                "Нет матчей для управления. Создай новый через «➕ Новый матч».",
                ephemeral=True
            )
            return
        emb = discord.Embed(
            title="🎮 Управление матчами",
            description="Выбери матч из списка, чтобы закрыть приём, завершить или посмотреть статистику.",
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(
            embed=emb, view=ManageMatchesSelectView(ms), ephemeral=True
        )

    @discord.ui.button(label="📊 Статистика", style=discord.ButtonStyle.secondary, row=0)
    async def btn_stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        emb = discord.Embed(
            title="📊 Статистика",
            description="Выбери раздел:",
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=emb, view=StatsRootView(), ephemeral=True)

    @discord.ui.button(label="📜 Логи", style=discord.ButtonStyle.secondary, row=0)
    async def btn_logs(self, interaction: discord.Interaction, button: discord.ui.Button):
        current = db.get_setting(LOG_CHANNEL_KEY)
        cur_str = f"<#{current}>" if current else "не настроен"
        emb = discord.Embed(
            title="📜 Канал логов",
            description=f"Текущий канал: {cur_str}\n\nВыбери канал из списка ниже — все ставки и завершения матчей будут дублироваться туда.",
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=emb, view=LogsView(), ephemeral=True)

    @discord.ui.button(label="🎯 Вид игрока", style=discord.ButtonStyle.danger, row=1)
    async def btn_player_view(self, interaction: discord.Interaction, button: discord.ui.Button):
        emb = discord.Embed(
            title="🎮 Панель игрока",
            description="Так игроки видят своё меню. Нажми на кнопку, чтобы проверить поведение.",
            color=discord.Color.blue(),
        )
        await interaction.response.send_message(embed=emb, view=PlayerMenuView(), ephemeral=True)


# ============================================================
# БУКМЕКЕР: СОЗДАНИЕ МАТЧА (МОДАЛКА)
# ============================================================
class NewMatchModal(discord.ui.Modal, title="Создание матча"):
    team1 = discord.ui.TextInput(label="Команда 1", placeholder="Team Liquid", max_length=50)
    team2 = discord.ui.TextInput(label="Команда 2", placeholder="Tundra Esports", max_length=50)
    heroes1 = discord.ui.TextInput(
        label="Герои команды 1", placeholder="Axe, Lina, Invoker", required=False,
        style=discord.TextStyle.paragraph, max_length=200,
    )
    heroes2 = discord.ui.TextInput(
        label="Герои команды 2", placeholder="QoP, Puck, Slark", required=False,
        style=discord.TextStyle.paragraph, max_length=200,
    )
    coefs = discord.ui.TextInput(
        label="Коэффициенты (через / или запятую)",
        placeholder="1.8 / 2.0",
        max_length=20,
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            parts = [p for p in re.split(r"[/,\s]+", self.coefs.value.strip()) if p]
            if len(parts) != 2:
                raise ValueError
            c1, c2 = float(parts[0]), float(parts[1])
            if c1 <= 1 or c2 <= 1:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "❌ Коэффициенты введи в формате `1.8 / 2.0`. Оба должны быть > 1.",
                ephemeral=True
            )
            return

        mid = db.create_match(
            self.team1.value.strip(), self.team2.value.strip(),
            self.heroes1.value.strip(), self.heroes2.value.strip(),
            c1, c2,
        )
        m = db.get_match(mid)

        emb = discord.Embed(
            title=f"✅ Матч #{mid} создан",
            description=format_match(m),
            color=discord.Color.green(),
        )
        view = PostMatchView(mid)
        await interaction.response.send_message(embed=emb, view=view, ephemeral=True)

        log_emb = discord.Embed(title="🆕 Новый матч", color=discord.Color.blurple())
        log_emb.add_field(name="Матч", value=f"#{mid}", inline=True)
        log_emb.add_field(name="Команда 1", value=f"{m['team1']} ×{m['coef1']:.2f}", inline=True)
        log_emb.add_field(name="Команда 2", value=f"{m['team2']} ×{m['coef2']:.2f}", inline=True)
        log_emb.add_field(name="Герои 1", value=m["heroes1"] or "—", inline=False)
        log_emb.add_field(name="Герои 2", value=m["heroes2"] or "—", inline=False)
        await log_to_channel(log_emb)


class PostMatchView(discord.ui.View):
    def __init__(self, match_id: int):
        super().__init__(timeout=300)
        self.match_id = match_id

    @discord.ui.button(label="📢 Опубликовать в канал", style=discord.ButtonStyle.success)
    async def btn_publish(self, interaction: discord.Interaction, button: discord.ui.Button):
        m = db.get_match(self.match_id)
        if not m:
            await interaction.response.send_message("❌ Матч не найден.", ephemeral=True)
            return
        if m["status"] != "open":
            await interaction.response.send_message("❌ Приём ставок уже закрыт.", ephemeral=True)
            return
        emb = discord.Embed(
            title=f"🎲 Матч #{m['id']}",
            description=format_match(m),
            color=discord.Color.blue(),
        )
        await interaction.channel.send(embed=emb, view=BetView(m))
        await interaction.response.send_message("✅ Опубликовано.", ephemeral=True)

    @discord.ui.button(label="📣 Позвать всех", style=discord.ButtonStyle.primary)
    async def btn_call(self, interaction: discord.Interaction, button: discord.ui.Button):
        m = db.get_match(self.match_id)
        if not m or m["status"] != "open":
            await interaction.response.send_message("❌ Матч не открыт.", ephemeral=True)
            return
        role = get_player_role(interaction.guild)
        if not role:
            await interaction.response.send_message(
                f"❌ Нет роли `{PLAYER_ROLE}` на сервере.", ephemeral=True
            )
            return
        emb = discord.Embed(
            title="🎲 Открыты ставки!",
            description=format_match(m),
            color=discord.Color.green(),
        )
        await interaction.channel.send(
            content=f"{role.mention} — новые ставки! 👇",
            embed=emb,
            view=BetView(m),
        )
        await interaction.response.send_message("📣 Позвали.", ephemeral=True)


# ============================================================
# БУКМЕКЕР: УПРАВЛЕНИЕ МАТЧАМИ
# ============================================================
class ManageMatchesSelectView(discord.ui.View):
    def __init__(self, matches):
        super().__init__(timeout=180)
        options = [
            discord.SelectOption(
                label=f"#{m['id']} · {m['team1']} vs {m['team2']}"[:100],
                value=str(m["id"]),
                description={"open": "🟢 открыт", "closed": "🟡 закрыт"}.get(m["status"], m["status"])[:100],
            )
            for m in matches[:25]
        ]
        self.select = discord.ui.Select(placeholder="Выбери матч…", options=options)
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction):
        mid = int(self.select.values[0])
        m = db.get_match(mid)
        if not m:
            await interaction.response.send_message("❌ Матч не найден.", ephemeral=True)
            return
        emb = discord.Embed(
            title=f"🎮 Управление матчем #{mid}",
            description=format_match(m),
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(
            embed=emb, view=ManageMatchActionsView(mid), ephemeral=True
        )


class ManageMatchActionsView(discord.ui.View):
    def __init__(self, match_id: int):
        super().__init__(timeout=300)
        self.match_id = match_id

    @discord.ui.button(label="🟡 Закрыть приём", style=discord.ButtonStyle.secondary)
    async def btn_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        m = db.get_match(self.match_id)
        if not m or m["status"] != "open":
            await interaction.response.send_message("❌ Матч уже не открыт.", ephemeral=True)
            return
        db.close_match(self.match_id)
        await interaction.response.send_message(f"🟡 Приём ставок на матч #{self.match_id} закрыт.", ephemeral=True)

    @discord.ui.button(label="🏁 Победила 1", style=discord.ButtonStyle.success)
    async def btn_finish1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(interaction, 1)

    @discord.ui.button(label="🏁 Победила 2", style=discord.ButtonStyle.danger)
    async def btn_finish2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(interaction, 2)

    async def _finish(self, interaction: discord.Interaction, winner: int):
        m = db.get_match(self.match_id)
        if not m:
            await interaction.response.send_message("❌ Матч не найден.", ephemeral=True)
            return
        if m["status"] == "finished":
            await interaction.response.send_message("❌ Матч уже завершён.", ephemeral=True)
            return
        payouts = db.finish_match(self.match_id, winner) or []
        winner_name = m["team1"] if winner == 1 else m["team2"]

        log_emb = discord.Embed(title="🏁 Матч завершён", color=discord.Color.red())
        log_emb.add_field(name="Матч", value=f"#{self.match_id}", inline=True)
        log_emb.add_field(name="Победитель", value=winner_name, inline=True)
        log_emb.add_field(name="Выплат", value=str(len(payouts)), inline=True)
        log_emb.add_field(name="Сумма выплат", value=str(sum(p for _, p in payouts)), inline=True)
        await log_to_channel(log_emb)

        if not payouts:
            await interaction.response.send_message(
                f"⚪ Матч #{self.match_id} завершён. Победила **{winner_name}**. Выигравших ставок нет.",
                ephemeral=True
            )
            return
        lines = [f"⚪ Матч #{self.match_id} завершён. Победила **{winner_name}**."]
        for uid, amount in payouts:
            lines.append(f"<@{uid}> → **+{amount}**")
        await interaction.channel.send("\n".join(lines))
        await interaction.response.send_message("✅ Выплаты произведены.", ephemeral=True)

    @discord.ui.button(label="📊 Статистика", style=discord.ButtonStyle.primary)
    async def btn_stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        s = db.match_stats(self.match_id)
        if not s:
            await interaction.response.send_message("❌ Матч не найден.", ephemeral=True)
            return
        m = s["match"]
        emb = discord.Embed(title=f"📊 Матч #{self.match_id}", color=discord.Color.gold())
        emb.add_field(name=m["team1"], value=f"ставок: {s['staked1']}", inline=True)
        emb.add_field(name=m["team2"], value=f"ставок: {s['staked2']}", inline=True)
        emb.add_field(name="Всего ставок", value=str(s["bets"]), inline=True)
        emb.add_field(name="Оборот", value=str(s["staked"]), inline=True)
        emb.add_field(name="Выплачено", value=str(s["paid"]), inline=True)
        emb.add_field(name="Прибыль", value=str(s["staked"] - s["paid"]), inline=True)
        await interaction.response.send_message(embed=emb, ephemeral=True)

    @discord.ui.button(label="📣 Позвать всех", style=discord.ButtonStyle.success)
    async def btn_call(self, interaction: discord.Interaction, button: discord.ui.Button):
        m = db.get_match(self.match_id)
        if not m or m["status"] != "open":
            await interaction.response.send_message("❌ Матч не открыт.", ephemeral=True)
            return
        role = get_player_role(interaction.guild)
        if not role:
            await interaction.response.send_message(
                f"❌ Нет роли `{PLAYER_ROLE}` на сервере.", ephemeral=True
            )
            return
        emb = discord.Embed(
            title="🎲 Открыты ставки!",
            description=format_match(m),
            color=discord.Color.green(),
        )
        await interaction.channel.send(
            content=f"{role.mention} — новые ставки! 👇",
            embed=emb,
            view=BetView(m),
        )
        await interaction.response.send_message("📣 Позвали.", ephemeral=True)


# ============================================================
# БУКМЕКЕР: СТАТИСТИКА
# ============================================================
class StatsRootView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="📊 Общая", style=discord.ButtonStyle.primary)
    async def btn_global(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = db.global_stats()
        emb = discord.Embed(title="📊 Общая статистика", color=discord.Color.gold())
        emb.add_field(name="Игроков", value=str(g["users"]), inline=True)
        emb.add_field(name="Матчей", value=f"{g['matches']} (завершено {g['finished']})", inline=True)
        emb.add_field(name="Ставок", value=str(g["total_bets"]), inline=True)
        emb.add_field(name="Оборот", value=str(g["total_staked"]), inline=True)
        emb.add_field(name="Выплачено", value=str(g["total_paid"]), inline=True)
        emb.add_field(name="Прибыль букмекера", value=str(g["profit"]), inline=True)
        await interaction.response.send_message(embed=emb, ephemeral=True)

    @discord.ui.button(label="🎯 По матчу", style=discord.ButtonStyle.secondary)
    async def btn_match(self, interaction: discord.Interaction, button: discord.ui.Button):
        ms = db.get_all_matches()
        if not ms:
            await interaction.response.send_message("Матчей пока нет.", ephemeral=True)
            return
        options = [
            discord.SelectOption(
                label=f"#{m['id']} · {m['team1']} vs {m['team2']}"[:100],
                value=str(m["id"]),
            )
            for m in ms[:25]
        ]
        select = discord.ui.Select(placeholder="Выбери матч…", options=options)

        async def cb(inter: discord.Interaction):
            s = db.match_stats(int(select.values[0]))
            if not s:
                await inter.response.send_message("❌ Матч не найден.", ephemeral=True)
                return
            m = s["match"]
            emb = discord.Embed(title=f"📊 Матч #{m['id']}", color=discord.Color.gold())
            emb.add_field(name=m["team1"], value=f"ставок: {s['staked1']}", inline=True)
            emb.add_field(name=m["team2"], value=f"ставок: {s['staked2']}", inline=True)
            emb.add_field(name="Всего", value=str(s["bets"]), inline=True)
            emb.add_field(name="Оборот", value=str(s["staked"]), inline=True)
            emb.add_field(name="Выплачено", value=str(s["paid"]), inline=True)
            emb.add_field(name="Прибыль", value=str(s["staked"] - s["paid"]), inline=True)
            await inter.response.send_message(embed=emb, ephemeral=True)

        select.callback = cb
        view = discord.ui.View(timeout=180)
        view.add_item(select)
        await interaction.response.send_message("Выбери матч:", view=view, ephemeral=True)

    @discord.ui.button(label="👤 По игроку", style=discord.ButtonStyle.secondary)
    async def btn_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        select = discord.ui.UserSelect(placeholder="Выбери игрока…")

        async def cb(inter: discord.Interaction):
            user = select.values[0]
            s = db.get_user_stats(user.id)
            rank = get_dota_rank(s["balance"])
            w, l = s["wins"], s["losses"]
            total = w + l
            wr = f"{w / total * 100:.1f}%" if total else "—"
            emb = discord.Embed(title=f"📊 {user.display_name}", color=discord.Color.gold())
            emb.add_field(name="Баланс", value=str(s["balance"]), inline=True)
            emb.add_field(name="Ранг", value=rank, inline=True)
            emb.add_field(name="Winrate", value=wr, inline=True)
            emb.add_field(name="Побед", value=str(w), inline=True)
            emb.add_field(name="Поражений", value=str(l), inline=True)
            emb.add_field(name="Всего ставок", value=str(s["total_bets"]), inline=True)
            emb.add_field(name="Поставлено", value=str(s["total_staked"]), inline=True)
            emb.add_field(name="Выиграно", value=str(s["total_won"]), inline=True)
            await inter.response.send_message(embed=emb, ephemeral=True)

        select.callback = cb
        view = discord.ui.View(timeout=180)
        view.add_item(select)
        await interaction.response.send_message("Выбери игрока:", view=view, ephemeral=True)


# ============================================================
# БУКМЕКЕР: ЛОГИ
# ============================================================
class LogsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        select = discord.ui.ChannelSelect(
            placeholder="Выбери канал для логов…",
            channel_types=[discord.ChannelType.text],
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        channel = interaction.data["values"][0]
        db.set_setting(LOG_CHANNEL_KEY, channel)
        await interaction.response.send_message(f"✅ Логи → <#{channel}>", ephemeral=True)

    @discord.ui.button(label="🔕 Отключить", style=discord.ButtonStyle.danger)
    async def btn_off(self, interaction: discord.Interaction, button: discord.ui.Button):
        db.set_setting(LOG_CHANNEL_KEY, "")
        await interaction.response.send_message("🔕 Логи отключены.", ephemeral=True)


# ============================================================
# КОМАНДЫ
# ============================================================
@tree.command(name="menu", description="[Букмекер] Панель управления матчами и статистикой")
async def menu(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message(
            "❌ Это панель букмекера. Игрокам — `/play`.",
            ephemeral=True
        )
        return
    emb = discord.Embed(
        title="💼 Панель букмекера",
        description="Управляй матчами, смотри статистику и настраивай логи.",
        color=discord.Color.gold(),
    )
    emb.set_footer(text=f"Роль: {ADMIN_ROLE}")
    await interaction.response.send_message(embed=emb, view=AdminMenuView(), ephemeral=True)


@tree.command(name="play", description="Личный кабинет: баланс, daily, матчи, ставки")
async def play(interaction: discord.Interaction):
    emb = discord.Embed(
        title="🎮 Личный кабинет",
        description="Баланс, ежедневный бонус, матчи и ставки — всё здесь.",
        color=discord.Color.blue(),
    )
    await interaction.response.send_message(embed=emb, view=PlayerMenuView(), ephemeral=True)


# ============================================================
# ЗАПУСК
# ============================================================
@client.event
async def on_ready():
    db.init_db()
    await tree.sync()
    for m in db.get_open_matches():
        client.add_view(BetView(m))
    await client.change_presence(activity=discord.Game(name="бурмалдатит бурмалду"))
    print(f"✅ Бот {client.user} запущен, views восстановлены")


client.run(TOKEN)