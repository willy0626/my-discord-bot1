import os
import discord
from discord import app_commands
from discord.ext import commands
from keep_alive import keep_alive

intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

# 用來記錄目前「應該要待在哪個語音頻道」字典（格式: {guild_id: channel_id}）
target_voice_channels = {}


@bot.event
async def on_ready():
  print(f"登入成功！目前身份：{bot.user}")
  try:
    synced = await bot.tree.sync()
    print(f"已同步 {len(synced)} 個斜線指令")
  except Exception as e:
    print(f"同步指令失敗: {e}")


# 監聽語音狀態變化（實現語音斷線自動重連/心跳維護）
@bot.event
async def on_voice_state_update(member, before, after):
  # 檢查是不是機器人自己
  if member.id == bot.user.id:
    guild_id = member.guild.id

    # 如果機器人原本在頻道裡，但現在變成 None（代表被踢出、斷線或被移動）
    if before.channel and after.channel is None:
      print(f"[語音斷線警報] 機器人從 {before.channel.name} 斷線了！")

      # 檢查我們是不是有紀錄這個伺服器原本要掛在哪個頻道
      if guild_id in target_voice_channels:
        channel_id = target_voice_channels[guild_id]
        channel = member.guild.get_channel(channel_id)

        if channel:
          try:
            await channel.connect()
            print(f"[自動重連成功] 已重新回到語音頻道：{channel.name}")
          except Exception as e:
            print(f"[重連失敗] 錯誤原因: {e}")


class VoiceSelectDropdown(discord.ui.Select):

  def __init__(self, channels):
    options = [
        discord.SelectOption(label=channel.name, value=str(channel.id))
        for channel in channels
    ]
    super().__init__(
        placeholder="請選擇要加入的語音頻道...",
        min_values=1,
        max_values=1,
        options=options,
    )

  async def callback(self, interaction: discord.Interaction):
    channel_id = int(self.values[0])
    channel = interaction.guild.get_channel(channel_id)
    guild_id = interaction.guild.id

    if channel and isinstance(channel, discord.VoiceChannel):
      # 記錄目標頻道，以便斷線時自動重連
      target_voice_channels[guild_id] = channel.id

      if interaction.guild.voice_client:
        await interaction.guild.voice_client.move_to(channel)
        await interaction.response.send_message(
            f"成功移動並鎖定語音頻道：**{channel.name}**（具備自動重連保護）",
            ephemeral=True,
        )
      else:
        try:
          await channel.connect()
          await interaction.response.send_message(
              f"成功加入語音頻道：**{channel.name}**，開始 1000 小時持久掛機！",
              ephemeral=True,
          )
        except Exception as e:
          await interaction.response.send_message(
              f"加入頻道失敗: {e}", ephemeral=True
          )
    else:
      await interaction.response.send_message(
          "找不到指定的語音頻道！", ephemeral=True
      )


class VoiceSelectView(discord.ui.View):

  def __init__(self, channels):
    super().__init__()
    self.add_item(VoiceSelectDropdown(channels))


@bot.tree.command(
    name="join", description="選擇一個語音頻道讓機器人加入並開啟持久掛機"
)
async def join(interaction: discord.Interaction):
  voice_channels = interaction.guild.voice_channels

  if not voice_channels:
    await interaction.response.send_message(
        "這個伺服器內沒有找到任何語音頻道！", ephemeral=True
    )
    return

  view = VoiceSelectView(voice_channels)
  await interaction.response.send_message(
      "請從下方選單選擇你要機器人進去的語音頻道：", view=view, ephemeral=True
  )


keep_alive()

TOKEN = os.getenv("DISCORD_TOKEN")
bot.run(TOKEN)
