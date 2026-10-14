# Pinky Cloud Telegram Bot — multiple paid plans

## Paid plans
- 1 Hour — ₹8
- 1 Day — ₹20
- 7 Days — ₹40
- 1 Month — ₹99 (30 days)
- 1 Year — ₹299 (365 days)

## Included
- Create Telegram share links for photos, videos, documents, audio, GIF/animation and voice
- Free users have a daily link limit
- Razorpay Payment Links and signed webhook verification
- Automatically activate the selected plan after successful payment
- User/file/payment records stored in SQLite
- Admin commands: /users, /set_free_limit, /addpaid, /ban, /unban, /broadcast

## Setup
1. Create a bot with @BotFather and get your numeric Telegram user ID.
2. Upload main.py, requirements.txt and render.yaml to a private GitHub repository.
3. Create a Render Web Service from that repository and choose Free if available.
4. Set these environment variables in Render:
   - BOT_TOKEN
   - OWNER_ID
   - PUBLIC_URL (your deployed HTTPS URL without trailing slash)
   - TELEGRAM_WEBHOOK_SECRET (random secret with letters/numbers/underscore/hyphen)
   - RAZORPAY_KEY_ID
   - RAZORPAY_KEY_SECRET
   - RAZORPAY_WEBHOOK_SECRET
   - FREE_LIMIT (default 5)
5. In Razorpay Dashboard → Settings → Webhooks, add:
   https://YOUR-SERVICE.onrender.com/razorpay-webhook
6. Configure the event `payment_link.paid`. Set a webhook secret and put the exact same secret in Render.
7. Deploy/redeploy. Open the bot and use /plans or /buy.
8. Test with Razorpay test keys before accepting real payments.

## Plan logic
Durations are seconds and prices are stored in paise in `main.py`. A verified payment activates the exact plan attached to the payment link. Duplicate webhook deliveries do not grant the same payment twice.

## Important storage warning
The bot currently uses SQLite (`bot.db`). Free cloud filesystems may be ephemeral; if the database is lost, file links and subscription records are lost too. For production, migrate to a persistent PostgreSQL database and check current provider limits/expiry. Do not promise permanent links if the bot database or Telegram file becomes unavailable.

Razorpay transaction fees/account requirements may apply. Never share bot tokens or payment secrets. Only share files you have permission to distribute.
