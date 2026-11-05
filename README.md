# Kunlik qo'shiq boti (Mureka + Claude + OpenAI + YouTube)

Bot har kuni avtomatik: **Claude** inglizcha qo'shiq matni va YouTube sarlavha/teglarini yozadi → **Mureka API** qo'shiqni yaratadi → **OpenAI** rasm chizadi → **ffmpeg** rasm+audiodan 1080p video yasaydi → **YouTube**'ga jadval bo'yicha joylaydi (standart: kuniga 3 ta, AQSh vaqti bilan).
Siz Telegram orqali sher yuborsangiz, u inglizcha qo'shiqqa aylantirilib navbatning **boshiga** qo'yiladi. Yubormasangiz, bot o'zi xilma-xil mavzularda qo'shiq yaratadi.

Faqat `requests` kutubxonasi kerak. ffmpeg Dockerfile ichida o'rnatiladi.

## 1. Kalitlar

| Nima | Qayerdan |
|---|---|
| `MUREKA_API_KEY` | platform.mureka.ai → API Keys |
| `ANTHROPIC_API_KEY` | console.anthropic.com |
| `OPENAI_API_KEY` | platform.openai.com (rasm uchun) |
| `TELEGRAM_BOT_TOKEN` | Telegram'da @BotFather → /newbot |
| `ADMIN_ID` | Telegram'da @userinfobot ga yozing, raqamini oling |
| `YT_CLIENT_ID`, `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN` | 2-bo'lim |

**Hech qaysi kalitni chatga yoki GitHub'ga yozmang.** Faqat Railway "Variables" bo'limiga.

## 2. YouTube ruxsati (bir marta)

1. console.cloud.google.com → yangi loyiha → **APIs & Services → Library → "YouTube Data API v3" → Enable**.
2. **OAuth consent screen**: User type *External*, ilova nomi ixtiyoriy, o'z Gmail'ingizni qo'shing. Keyin **"Publish app" (In production)** tugmasini bosing.
   ⚠️ Ilova "Testing" holatida qolsa, refresh token **7 kundan keyin o'ladi** va bot yuklay olmay qoladi.
3. **Credentials → Create credentials → OAuth client ID → Application type: Desktop app**. Client ID va Secret'ni oling.
4. O'z kompyuteringizda:
   ```
   pip install requests
   python3 auth_youtube.py
   ```
   Brauzerda **musiqa kanali egasi bo'lgan Google akkauntni** tanlang. "Google hasn't verified this app" chiqsa: Advanced → Continue. Terminalda chiqqan `YT_REFRESH_TOKEN` ni saqlang.
5. ⚠️ **Yangi (tekshirilmagan) API loyihasidan yuklangan videolar YouTube tomonidan avtomatik "private" qilib qo'yilishi mumkin.** Bunda YouTube'ning "API Services audit" formasini to'ldirib, loyihani tekshiruvdan o'tkazish kerak. Birinchi videodan keyin kanalda ko'rinishini tekshiring.

## 3. Railway'ga joylash

1. Bu papkani yangi GitHub repoga yuklang (`.env` yuklanmaydi, `.gitignore` himoya qiladi).
2. Railway → New Project → Deploy from GitHub repo. Dockerfile avtomatik topiladi.
3. **Volume qo'shing** va mount path'ni `/data` qiling. Busiz har deploy'da navbat va videolar o'chib ketadi.
4. Variables bo'limiga `.env.example` dagi o'zgaruvchilarni kiriting (majburiylari: yuqoridagi jadval).
5. Deploy. Telegram'da botga `/start` yozing, "Bot ishga tushdi" xabari kelishi kerak.

## 4. Ishlatish

- **Sher yuborish:** oddiy matn yuboring (o'zbekcha ham bo'ladi). Bot inglizcha qo'shiqqa aylantiradi.
- `/holat` — navbat, keyingi joylash, bugungi sarf
- `/navbat` — oxirgi 10 qo'shiq
- `/yarat [mavzu]` — hoziroq yangi qo'shiq
- `/joyla` — tayyor videoni hoziroq joylash
- `/bekor <id>` — qo'shiqni bekor qilish
- `/toxtat`, `/davom` — avtomatikani to'xtatish/yoqish

Sozlamalar (Railway Variables): `POST_TIMES=08:00,13:00,19:00` (soni = kuniga nechta), `TIMEZONE=America/New_York`, `STOCK_TARGET` (zaxirada nechta tayyor video), `MAX_MUREKA_PER_DAY=12` (kunlik pul sarfi chegarasi), `YT_PRIVACY` (public/unlisted/private).

## 5. Narx (taxminiy)

Mureka qo'shiq ~$0,03–0,05 (`MUREKA_N=1`), Claude matni ~bir necha sent, OpenAI rasm ~bir necha sent. Bitta video ~$0,10 atrofida, kuniga 3 ta bo'lsa oyiga ~$9–12. `MAX_MUREKA_PER_DAY` xato bo'lib takroriy so'rovlar pulni yeb qo'ymasligini ta'minlaydi.

## 6. Muhim ogohlantirishlar

- **Takroriy kontent:** YouTube bir xil shablonli ommaviy videolarni monetizatsiyadan chiqarishi mumkin. Bot har safar turli janr, mavzu va rasm uslubini tanlaydi, lekin kanalni kuzatib boring. Alohida musiqa kanalida ishlating, asosiy walking-tour kanalida emas.
- **AI belgisi:** `YT_SYNTHETIC_MEDIA=1` videolarni "AI yaratgan" deb belgilaydi va tavsifga ham yozadi. O'chirmang.
- **Matn huquqi:** sher yuborsangiz, u o'zingizniki yoki erkin foydalanishga ruxsat berilgani bo'lishi kerak. Mavjud mashhur qo'shiq so'zlarini yubormang.
- **Mureka litsenziyasi:** API orqali yaratilgan qo'shiqlarni YouTube'da monetizatsiya qilish shartlarini platform.mureka.ai Terms'idan o'qing.
- **$10 Trial** bir vaqtda 1 ta so'rovni ruxsat beradi. Bot qo'shiqlarni ketma-ket yaratadi, shuning uchun muammo yo'q.
- Mureka'da model nomi `MUREKA_MODEL=auto`. Aniq model (V9, V7.6 va h.k.) kerak bo'lsa, hujjatdagi aniq nomini shu o'zgaruvchiga yozing.

## 7. Mahalliy test

```
python3 tests/smoke_test.py
```
Haqiqiy kalitlarsiz, soxta API javoblari bilan butun zanjirni (matn → qo'shiq → rasm → video → YouTube) tekshiradi. ffmpeg o'rnatilgan bo'lishi kerak.
