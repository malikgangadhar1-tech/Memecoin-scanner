"""Known pump.fun farm deployers (Rohit Oct 8 2026 22:46 IST: label only, "2% ain't worth it").
Top 25 pump.fun deployers by extracted PnL per @bandosei (x.com/bandosei/status/2108215385397059797, Oct 8 2026):
770k launches, $128M extracted, ~1% graduate. LABEL ONLY: never a filter until graded data proves it."""
FARM = {
"bwamJzztZsepfkteWRChggmXuiiCQvpLqPietdNfSXa","whamNNP9tHoxLg92yHvJPdYhghEoCg1qYTsh5a2oLbx",
"7naFFwuEJWeWwWYQUkgAWHsxYKg3KctEuUj42JdAMidP","GpTXmkdvrTajqkzX1fBmC4BUjSboF9dHgfnqPqj8WAc4",
"5FqUo9aBjsp7QeeyN6Vi2ZmF2fjS4H5EU7wnAQwPy17z","8ZN71XTdVo8yRovnGLmNgW3Tgniw6A4J3JGLvPD686FP",
"AV7PjXHL5JXZ1YoYRoN9Dsstg1x2UciBupMCXcJP8gUz","D9gQ6RhKEpnobPBUdWY5bPQt2p3zGk3iVz6ChpUi2ArA",
"EBx24uAPtaS1SvHwRhKEktSgzaXdiVEyHuSVpAMVwrcD","Aqje5DsN4u2PHmQxGF9PKfpsDGwQRCBhWeLKHCFhSMXk",
"8i5U2uNBEuTc4zskYP14zbebDg2RSwrrG8REhEnJb97K","GdRSPexhxbQz5H2zFQrNN2BAZUqEjAULBigTPvQ6oDMP",
"8NJ7Ujpji8uMF2675mqaTSEm2DCbfJA7fiRKtiaqkaLN","GeUnv1jmtviRbR7Gu1JnXSGkUMUgFVBHuEVQVpTaUX1W",
"DC99qH3jXiq5pPWQd6PjjJcCxTV593s58CLPxWGpEywt","GZVSEAajExLJEvACHHQcujBw7nJq98GWUEZtood9LM9b",
"7GhWwhaMgbKiRWxF93Bud6HnHMci6NCLTJyTxG8zFH51","G7NvZKjoVqBDWciSYtWWgUPB7DA1iJavdvH5jty2FAmM",
"MNhBbrscBPmeid54buiqSgyWa4D8PY6uKHoK2wJsTJN","B9Zbs2W9VK22AHnCWiK4PqBueDFzN17RNAFu5uFozLMJ",
"36DWP52MVRDooYNrcRVDyoCh2R1fPXCYqKJQYg9pFQoE","83QQFLxcEzuJZaejBnzAiW5tyfyaRHMLZnkhJbRPRWtf",
"5Hyf6srdbrrD1HLjqxcFbXJRFBHgnwoZc7aZRj8vDuk5","D3sovmjANgA8V27e5rnQft8kV3mocSH8gw9zwth3Ba9g",
"6d22FozaKK239PoBYVffkYKA1QPQZE8fC7AQkpmHQfjp",
}
def is_farm(w): return bool(w) and w in FARM
