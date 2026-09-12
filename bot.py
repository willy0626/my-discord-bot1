import discord
from discord.ext import commands
import os
from keep_alive import keep_alive

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"成功登入！目前身份：{bot.user}")

@bot.command()
async def ping(ctx):
    await ctx.send("Pong! 24小時掛機中 🚀")

# 啟動保持活力的 Flask 網頁
keep_alive()

# 建議正式上線時用環境變數，或直接貼上你的 Token 測試
TOKEN = os.getenv("DISCORD_TOKEN", "MTU0ODM0MDc4NTY2MDAzNTIwMg.GKeRE8.ULw0q9Yw02rGCqOF3UpDmUkQjSSmoc0fi6gYAg")
bot.run(TOKEN)