import asyncio
import logging
import os
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv

from captcha import render_captcha
from challenges import ChallengeError, ChallengeStore, TTL, Verdict

log = logging.getLogger(__name__)


def verified_role(guild: discord.Guild, role_id: int) -> discord.Role:
    role = guild.get_role(role_id)
    member = guild.me
    if role is None or role.is_default() or role.managed:
        raise ChallengeError("Администратору нужно указать обычную роль для верификации.")
    permissions = role.permissions
    if any((permissions.administrator, permissions.manage_guild,
            permissions.manage_roles, permissions.manage_channels,
            permissions.manage_webhooks, permissions.ban_members,
            permissions.kick_members, permissions.moderate_members,
            permissions.manage_messages, permissions.manage_threads,
            permissions.manage_events, permissions.mention_everyone)):
        raise ChallengeError("Для верификации нельзя использовать роль модератора.")
    if member is None or not member.guild_permissions.manage_roles:
        raise ChallengeError("Боту нужно право «Управлять ролями».")
    if role >= member.top_role:
        raise ChallengeError("Роль бота должна находиться выше роли верификации.")
    return role


async def notify(interaction: discord.Interaction, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


async def component_error(interaction: discord.Interaction, error: Exception) -> None:
    log.error("Component failed: %s", type(error).__name__)
    await notify(interaction, "Не удалось выполнить проверку. Попробуйте снова или обратитесь к администратору.")


class SafeView(discord.ui.View):
    async def on_error(self, interaction, error, item):
        await component_error(interaction, error)


class CodeModal(discord.ui.Modal, title="Введите код с картинки"):
    code = discord.ui.TextInput(label="Код из 6 символов", min_length=6, max_length=6)

    def __init__(self, bot: "VerificationBot", user_id: int, nonce: str):
        super().__init__(timeout=TTL)
        self.bot = bot
        self.user_id = user_id
        self.nonce = nonce

    async def on_submit(self, interaction: discord.Interaction):
        if (interaction.guild_id != self.bot.guild_id
                or interaction.user.id != self.user_id
                or not isinstance(interaction.user, discord.Member)):
            await notify(interaction, "Эта проверка вам недоступна.")
            return
        try:
            role = verified_role(interaction.guild, self.bot.role_id)
        except ChallengeError as error:
            await notify(interaction, str(error))
            return
        verdict = self.bot.challenges.check(
            interaction.guild_id, interaction.user.id, self.nonce, str(self.code),
        )
        messages = {
            Verdict.WRONG: "Неверный код. Нажмите «Ввести код» под картинкой и попробуйте ещё раз.",
            Verdict.EXHAUSTED: "Попытки закончились. Получите новую капчу на панели верификации.",
            Verdict.EXPIRED: "Капча истекла или уже использована. Получите новую на панели верификации.",
        }
        if verdict != Verdict.PASSED:
            await notify(interaction, messages[verdict])
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await interaction.user.add_roles(role, reason="Passed verification CAPTCHA")
        except discord.Forbidden:
            await notify(interaction, "Нет прав на выдачу роли. Сообщите администратору; после исправления получите новую капчу.")
        except discord.HTTPException:
            await notify(interaction, "Discord не подтвердил выдачу роли. Проверьте свои роли и при необходимости пройдите проверку заново.")
        else:
            await notify(interaction, "✅ Проверка пройдена! Роль выдана, добро пожаловать.")

    async def on_error(self, interaction, error):
        await component_error(interaction, error)


class AnswerView(SafeView):
    def __init__(self, bot: "VerificationBot", user_id: int, nonce: str):
        super().__init__(timeout=TTL)
        self.bot, self.user_id, self.nonce = bot, user_id, nonce

    @discord.ui.button(label="Ввести код", style=discord.ButtonStyle.primary)
    async def answer(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await notify(interaction, "Это чужая проверка.")
            return
        await interaction.response.send_modal(CodeModal(self.bot, self.user_id, self.nonce))


class VerificationPanel(SafeView):
    def __init__(self, bot: "VerificationBot"):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Пройти проверку", emoji="🛡️", style=discord.ButtonStyle.success,
        custom_id="verifybot:begin:v1",
    )
    async def begin(self, interaction: discord.Interaction, button: discord.ui.Button):
        if (interaction.guild_id != self.bot.guild_id
                or not isinstance(interaction.user, discord.Member)):
            await notify(interaction, "Проверка доступна только на настроенном сервере.")
            return
        try:
            role = verified_role(interaction.guild, self.bot.role_id)
            if role in interaction.user.roles:
                await notify(interaction, "Вы уже прошли проверку ✅")
                return
            challenge = self.bot.challenges.issue(interaction.guild_id, interaction.user.id)
        except ChallengeError as error:
            await notify(interaction, str(error))
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        picture = await asyncio.to_thread(render_captcha, challenge.answer)
        embed = discord.Embed(
            title="Проверка участника",
            description="Введите символы с картинки. Регистр не важен.\nСрок: 2 минуты · Попыток: 3.\nНовая капча отменяет предыдущую.",
            color=0x5865F2,
        )
        embed.set_image(url="attachment://captcha.png")
        await interaction.followup.send(
            embed=embed, file=discord.File(picture, filename="captcha.png"),
            view=AnswerView(self.bot, interaction.user.id, challenge.nonce), ephemeral=True,
        )


class VerificationBot(discord.Client):
    def __init__(self, guild_id: int, role_id: int):
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.guild_id = guild_id
        self.role_id = role_id
        self.challenges = ChallengeStore()
        self.tree = app_commands.CommandTree(self)
        self.tree.on_error = self.command_error

    async def setup_hook(self):
        self.add_view(VerificationPanel(self))

        @self.tree.command(
            name="verification-panel", description="Разместить панель верификации",
            guild=discord.Object(id=self.guild_id),
        )
        @app_commands.guild_only()
        @app_commands.default_permissions(administrator=True)
        @app_commands.checks.has_permissions(administrator=True)
        async def panel(interaction: discord.Interaction, channel: discord.TextChannel):
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                verified_role(interaction.guild, self.role_id)
            except ChallengeError as error:
                await notify(interaction, str(error))
                return
            if channel.guild.id != self.guild_id:
                await notify(interaction, "Выберите канал настроенного сервера.")
                return
            permissions = channel.permissions_for(interaction.guild.me)
            if not all((permissions.view_channel, permissions.send_messages,
                        permissions.embed_links, permissions.attach_files)):
                await notify(interaction, "В канале боту нужны права: просмотр, отправка сообщений, ссылки и прикрепление файлов.")
                return
            embed = discord.Embed(
                title="🛡️ Добро пожаловать!",
                description="Чтобы получить доступ к серверу, нажмите кнопку и решите капчу.\nКартинку и ответ видите только вы.\nЕсли картинка недоступна вам, обратитесь к модератору.",
                color=0x57F287,
            )
            await channel.send(embed=embed, view=VerificationPanel(self))
            await notify(interaction, f"Панель размещена в {channel.mention}.")

        await self.tree.sync(guild=discord.Object(id=self.guild_id))

    async def command_error(self, interaction, error):
        if isinstance(error, app_commands.CheckFailure):
            await notify(interaction, "Эта команда доступна только администратору сервера.")
        else:
            await component_error(interaction, error)

    async def on_ready(self):
        log.info("Verification bot connected as %s", self.user.id)


def main():
    load_dotenv(Path(__file__).resolve().parent / ".env")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token or token == "put_your_bot_token_here":
        raise SystemExit("Укажите DISCORD_TOKEN в .env.")
    ids = []
    for name in ("GUILD_ID", "VERIFIED_ROLE_ID"):
        raw = os.getenv(name, "").strip()
        if not raw.isascii() or not raw.isdecimal() or int(raw) <= 0:
            raise SystemExit(f"{name} должен быть положительным числовым ID в .env.")
        ids.append(int(raw))
    try:
        VerificationBot(guild_id=ids[0], role_id=ids[1]).run(token, log_handler=None)
    except discord.LoginFailure:
        raise SystemExit("Discord отклонил токен. Проверьте DISCORD_TOKEN в .env.") from None


if __name__ == "__main__":
    main()
