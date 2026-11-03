# Спутники

Прошивка хранит до 25 спутников с TLE для предсказания прохождений. Загрузчик
`satellites/load_satellites.py` — отдельный CLI, он не встроен в TUI: читает
`satellites/satellites.json`, скачивает свежие TLE с Celestrak по NORAD ID и
записывает их в рацию.

**После записи рацию нужно перезагрузить** — блок читается из flash только при
старте.

```bash
# записать спутники из satellites.json (TLE скачаются автоматически)
venv/bin/python satellites/load_satellites.py

# посмотреть текущие спутники в рации
venv/bin/python satellites/load_satellites.py --check

# другой файл конфигурации
venv/bin/python satellites/load_satellites.py my_sats.json
```

## Формат satellites.json

Массив объектов:

|Поле|Описание|
|:---|:---|
|`name`|до 8 ASCII символов — имя в меню рации|
|`norad_id`|NORAD каталог — по нему качаются TLE с Celestrak|
|`tle1` / `tle2`|TLE вручную: стандартные 69-символьные строки. `null` = скачать с Celestrak|
|`rx_freq_mhz` / `tx_freq_mhz`|голосовая пара (downlink/uplink), МГц|
|`ctcss_tx_hz`|TX CTCSS, Гц (null = нет)|
|`arm_ctcss_hz`|армирующий тон (SO-50: 74.4), Гц|
|`secondary_rx/tx_freq_mhz`|APRS пара, МГц|
|`tertiary_rx/tx_freq_mhz`|доп. пара, МГц|

Незаполненные поля — `null`. Если `tle1`/`tle2` заданы (не null), они
используются как есть, и скачивание с Celestrak для этого спутника не
выполняется — удобно при офлайн-работе или для тестовых кеплеров.

## Где это лежит во flash

Custom-data регион `@0x20000`, блок `SATELLITE_TLE` (type=3), 25 записей по
100 байт: name[8] + сжатая TLE line1[12] + line2[28] + частоты + CTCSS.
TLE пакуются по 2 символа на байт через LUT `"0123456789. +-*"`
(`satellite.c` прошивки). Формат совпадает с реализацией
dondch/chirp-opengd77-aes. Скрипт перезаписывает блок по месту, трогая только
изменившиеся 4К-сектора.
