# Ветка 3. Автоматический выбор канала по IP

## Цель

Организовать на OpenWrt таблицу на 300-500 IP-адресов. Для каждого IP хранить:

- IP;
- время последнего обращения;
- текущий выбранный VPN/proxy-канал;
- результаты последней проверки скорости по каналам.

При обращении к новому IP система должна быстро выбрать лучший канал, добавить IP в таблицу и направить трафик через выбранный канал. Если места нет, удаляется IP, к которому дольше всего не обращались.

## Важное ограничение

Это самая сложная и рискованная ветка. Ее нельзя сразу внедрять в рабочий PBR. Сначала нужен прототип, который только собирает данные и строит решения, но не ломает маршрутизацию.

## Базовая архитектура

```text
Новый IP
  -> lookup в таблице
  -> если IP найден: обновить last_seen и использовать выбранный channel
  -> если IP не найден:
       проверить доступные каналы
       выбрать лучший
       добавить IP в таблицу
       добавить IP в nft-set выбранного канала
```

Периодический процесс:

```text
раз в минуту
  -> взять активные IP из таблицы
  -> перепроверить каналы ограниченной пачкой
  -> при необходимости переложить IP между nft sets
```

## Каналы

Начальный набор:

- `awg1`;
- `awg2`;
- будущий `lokvpn`;
- возможно прямой `wan`.

## Метрики скорости

Начать с простых метрик:

- ping через интерфейс, если IP отвечает на ICMP;
- TCP connect time на порт 443;
- fallback: проверка доступности через маршрут.

Ping сам по себе недостаточен, потому что многие IP не отвечают на ICMP. Поэтому для HTTPS-ресурсов TCP connect time важнее.

## Таблица

Первый вариант хранения:

```text
/tmp/cudy-channel-selector.db
```

Формат на старте может быть TSV:

```text
ip	last_seen	channel	latency_awg1	latency_awg2	latency_lokvpn	updated_at
```

Если логика усложнится, перейти на SQLite.

## Интеграция с nft/PBR

Возможная схема:

- для каждого канала есть nft-set;
- скрипт добавляет IP в set выбранного канала;
- если выбранный канал меняется, IP удаляется из старого set и добавляется в новый;
- PBR или отдельные nft rules направляют set в нужную таблицу маршрутизации.

## Главная техническая проблема

Нужно понять, как именно ловить "новый IP, к которому обратился клиент":

- через nft counters/log;
- через conntrack;
- через dnsmasq/nftset;
- через eBPF не рассматриваем на Cudy, слишком тяжело;
- через периодический анализ connections как первый прототип.

Самый прагматичный старт - читать conntrack и выделять destination IP для активных соединений LAN-клиентов.

## План работ

1. Прототип наблюдения:
   - читать текущие соединения;
   - выделять destination IP;
   - обновлять `last_seen`;
   - ничего не менять в маршрутизации.

2. Прототип проверки каналов:
   - для одного IP проверить `awg1` и `awg2`;
   - получить latency;
   - выбрать лучший канал.

3. Прототип таблицы:
   - лимит 300-500 записей;
   - LRU-удаление;
   - сохранение в `/tmp`.

4. Интеграция с nft sets:
   - добавлять IP в выбранный set;
   - удалять из остальных sets;
   - проверять, что PBR корректно маршрутизирует.

5. Защита от флаппинга:
   - не переключать IP слишком часто;
   - вводить hysteresis, например переключать только если новый канал быстрее на 20-30%;
   - cooldown на IP.

6. Наблюдаемость:
   - команда `selector status`;
   - команда `selector show IP`;
   - лог последних переключений.

## Результат ветки

- Рабочий прототип автоселектора.
- Таблица IP с LRU.
- Контролируемая интеграция с nft/PBR.
- Возможность отключить автоселектор и вернуться к ручному `vpn1`/`vpn2`.

## Первый следующий шаг

На OpenWrt проверить доступность инструментов:

```sh
command -v conntrack
command -v nft
command -v awk
command -v nc
command -v curl
```

И посмотреть активные соединения:

```sh
conntrack -L 2>/dev/null | head -30
```

## Результат первичной проработки

Первый этап должен быть только наблюдателем:

- читать активные destination IP из `conntrack`;
- фильтровать только LAN-клиентов и внешние IPv4;
- поддерживать таблицу состояния в `/tmp`;
- проверять только небольшую пачку новых IP за цикл;
- выбирать лучший канал по метрике;
- писать dry-run лог;
- не менять PBR и nft-set.

Рекомендуемый scope v0.1:

```text
read conntrack every 10s
extract public IPv4 dst from LAN clients
maintain /tmp TSV state, max 500 entries
measure only 3-5 new IPs per cycle
channels: awg1, awg2
metrics: ping + TCP 443 with timeout
output: logger + TSV
no nft writes
no PBR changes
```

Проверить инструменты на OpenWrt:

```sh
command -v conntrack nft awk nc curl
command -v ip ping timeout flock logger
conntrack -L -f ipv4 2>/dev/null | head -30
```

Таблица v0.1:

```text
ip	last_seen	selected_channel	score_awg1	score_awg2	rtt_awg1_ms	rtt_awg2_ms	tcp_awg1_ms	tcp_awg2_ms	status	updated_at
```

Файлы прототипа:

```text
/tmp/auto-channel-selector.tsv
/tmp/auto-channel-selector.queue
/tmp/auto-channel-selector.log
```

Что не делать на первом этапе:

- не менять рабочие PBR-правила;
- не очищать существующие nft-set;
- не вызывать `vpn1`/`vpn2`;
- не включать nft logging в боевых chains;
- не делать массовые проверки сотен IP без rate limit;
- не писать часто во flash;
- не переключать канал при малой разнице метрик.

## Диагностика 2026-05-26

На Cudy проверены инструменты:

Доступны:

- `nft`
- `awk`
- `nc`
- `ip`
- `ping`
- `flock`
- `logger`
- `apk`

Отсутствуют:

- `conntrack`
- `curl`
- `timeout`
- `opkg`

Следствие:

- dry-run v0.1 через `conntrack` пока невозможен без установки пакета;
- для установки использовать `apk`, не `opkg`;
- если пакет `conntrack` доступен в репозиториях, первым шагом ветки 3 будет установка только этого инструмента и повторная read-only проверка;
- если `timeout` нужен, искать пакет с GNU/coreutils timeout или заменить таймауты возможностями `nc -w` и короткими `ping -W`.

## Подготовлено на Cudy 2026-05-26

Установлены пакеты:

- `conntrack`
- `curl`
- `coreutils-timeout`

Теперь доступны:

- `/usr/sbin/conntrack`
- `/usr/bin/curl`
- `/usr/bin/timeout`

Проверка:

- `conntrack -L -f ipv4` показывает активные соединения LAN-клиентов;
- можно переходить к dry-run прототипу v0.1 без изменения PBR/nft.

## Override-списки до автоселектора 2026-05-26

До полноценного автоселектора добавлен простой и безопасный механизм приоритетов:

```text
/etc/pbr-overrides/force-wan.domains
/etc/pbr-overrides/force-wan.ips
/etc/pbr-overrides/force-vpn.domains
/etc/pbr-overrides/force-vpn.ips
```

Эти файлы обрабатываются во время каждого `/etc/init.d/pbr restart`, в том числе ежедневного cron-обновления.

Логика приоритета:

```text
force-wan -> pbr_wan_4_dst_ip_user
force-vpn -> pbr_<TARGET_INTERFACE>_4_dst_ip_user
big antifilter/opencck list -> pbr_<TARGET_INTERFACE>_4_dst_ip_user
default -> wan
```

Важно: `force-wan` не вычитается из большого списка. Вместо этого адреса добавляются в WAN-set, а правило WAN-set стоит раньше AWG-set. Поэтому даже если один и тот же IP есть в обоих set, выигрывает WAN.

Это промежуточная архитектура между ручными исключениями и будущим автоселектором. Ветка 3 позже может использовать такие же приоритетные set-ы:

- `force-wan` как постоянный запрет VPN для чувствительных сайтов;
- `force-vpn` как постоянный запрет прямого WAN;
- динамические set-ы автоселектора ниже этих ручных приоритетов.

## Замена широкого OpenCCK all-data 2026-05-28

`rutube.ru` попал в туннель, хотя должен идти напрямую. Вероятный источник мусора - широкий OpenCCK all-data URL без фильтров:

```text
https://iplist.opencck.org/ru/?format=text&data=cidr4
```

Решение для следующей версии PBR-скрипта:

- убрать OpenCCK all-data из базового списка;
- оставить базой только `antifilter/allyouneed.lst`;
- OpenCCK использовать только явно через `force-vpn.urls`, например с `site=` или `group=`;
- российские сервисы, которые должны открываться напрямую, держать в `force-wan.domains` / `force-wan.ips`.

В локальный артефакт `openwrt/pbr.user.opencck-merged-vpn` уже внесена эта схема. Добавлены:

```text
force-wan.urls
force-vpn.urls
```

Для Rutube добавлены WAN-домены:

```text
rutube.ru
www.rutube.ru
static.rutube.ru
pic.rutube.ru
cdn.rutube.ru
```

Применено на Cudy 2026-05-28:

```text
backup: /root/backup-before-no-opencck-20260528-080149
```

Подтверждение источника мусора:

- `109.238.90.239` для `rutube.ru` содержался в `pbr_awg2_4_dst_ip_user` как `109.238.88.0/22`;
- `109.238.88.0/22` найден в `/tmp/pbr_opencck_cidr4.raw`;
- в `antifilter` совпадения для этого IP не было.

После удаления OpenCCK all-data:

```text
109.238.90.239 -> pbr_wan_4_dst_ip_user, not pbr_awg2_4_dst_ip_user
178.248.233.148 -> pbr_wan_4_dst_ip_user, not pbr_awg2_4_dst_ip_user
89.248.230.8 -> pbr_wan_4_dst_ip_user, not pbr_awg2_4_dst_ip_user
```

Вывод для будущего автоселектора: широкие внешние списки нельзя подключать без whitelist/curated-фильтра. Ручные `force-wan` / `force-vpn` должны оставаться приоритетнее динамики.

## OpenAI/ChatGPT как force-vpn 2026-05-28

Удаление широкого OpenCCK также убрало часть маршрутов, через которые Codex/ChatGPT раньше попадал в туннель. Для восстановления добавлены ручные `force-vpn` entries:

```text
chatgpt.com
ab.chatgpt.com
auth.openai.com
api.openai.com
openai.com
platform.openai.com
cdn.oaistatic.com
persistent.oaistatic.com
oaistatic.com
files.oaiusercontent.com
oaiusercontent.com
```

Также добавлены текущие IPv4 резолвы в `force-vpn.ips`, чтобы не ждать следующего DNS-обновления:

```text
8.6.112.0/32
8.47.69.0/32
104.18.33.45/32
104.18.41.241/32
162.159.140.245/32
172.64.146.15/32
172.64.154.211/32
172.66.0.243/32
```

Вывод: после удаления broad source нужно переносить критичные сервисы в curated `force-vpn`, иначе они уйдут в default WAN.

## WAN-RU + OpenCCK split 2026-05-28

Предыдущий вывод уточнен: полностью удалять OpenCCK было слишком грубо. Новая схема применена на Cudy:

```text
backup: /root/backup-before-wan-ru-opencck-20260528-090634
script: /usr/share/pbr/pbr.user.opencck-merged-vpn
```

VPN base:

```text
https://antifilter.download/list/allyouneed.lst
https://iplist.opencck.org/ru/?format=text&data=cidr4
```

WAN-RU base:

```text
https://www.ipdeny.com/ipblocks/data/aggregated/ru-aggregated.zone
https://raw.githubusercontent.com/v2fly/domain-list-community/master/data/category-ru
```

После первого запуска:

```text
/tmp/pbr_wan.clean: 8663
/tmp/pbr_opencck_merged_vpn.clean: 18352
```

Проверки:

```text
109.238.88.0/22   -> wan=yes, awg2=no
8.6.112.0/32      -> wan=no,  awg2=yes
8.47.69.0/32      -> wan=no,  awg2=yes
185.169.155.85/32 -> wan=yes, awg2=no
```

Реальный conntrack с ПК `192.168.8.102`:

```text
chatgpt.com -> reply dst 10.8.1.10
rutube.ru -> reply dst 192.168.1.174
vseinstrumenti.ru -> reply dst 192.168.1.174
```

## Gemini / Google AI route fix 2026-05-28

Gemini showed unsupported country. awg2 itself was confirmed as US:

```text
45.39.33.103 / United States / New York
```

Main `gemini.google.com` IPs were already in awg2, but conntrack showed `142.251.1.188` (`lb-in-f188.1e100.net`) going via WAN. Added Gemini/Google AI domains to `force-vpn.domains` and `142.251.1.188/32` to `force-vpn.ips`, then restarted PBR and deleted old conntrack entries.

Post-fix conntrack samples from `192.168.8.102`:

```text
142.251.150.2 -> reply dst 10.8.1.10
173.194.222.95 -> reply dst 10.8.1.10
64.233.162.95 -> reply dst 10.8.1.10
209.85.233.84 -> reply dst 10.8.1.10
```

## google-check helper 2026-05-28

Added `openwrt/google-check` and installed it as `/usr/bin/google-check` on Cudy.

It collects a compact Google/Gemini routing matrix for `eth0`, `awg1`, and `awg2`:

```text
ifconfig.co geo
ipinfo.io geo
Google Search html_lang
Gemini HTTP shell status
Google One HTTP shell status
recent Google/Gemini conntrack from PC 192.168.8.102
```

Initial observations:

```text
eth0 -> RU, google_html_lang=ru
awg1 -> ifconfig=ES, ipinfo=KZ/Almaty, google_html_lang=kk
awg2 -> US, google_html_lang=en
```

## Fast vpn1/vpn2 switchers 2026-05-28

Old switchers performed full PBR restart on every channel switch, causing a long pause at `Running /usr/share/pbr/pbr.user.opencck-merged-vpn` and leaving old client conntrack entries alive.

Installed updated switchers:

```text
/usr/bin/vpn-switch
/usr/bin/vpn1
/usr/bin/vpn2
```

Fast path:

- change persisted `TARGET_INTERFACE`;
- reuse cached `/tmp/pbr_opencck_merged_vpn.clean` and `/tmp/pbr_wan.clean`;
- batch nft updates through one `nft -f`;
- flush conntrack for `192.168.8.0/24` and `10.77.0.0/24`.

Control result:

```text
vpn2 real time: 4.41s
142.251.153.2/32 -> awg2=yes
109.238.88.0/22 -> wan=yes
```
