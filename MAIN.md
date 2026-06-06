# Главный план проекта Cudy TR3000

Этот файл - общий центр проекта. Он фиксирует текущее состояние, связи между ветками и порядок внедрения.

## Текущая точка

- Cudy TR3000 работает на OpenWrt.
- Первый AmneziaWG-туннель `awg1` поднят и используется PBR.
- Второй туннель `awg2` добавлен, виден в PBR и уже проверялся тестовым правилом.
- Большой список маршрутизируемых IP грузится через `/usr/share/pbr/pbr.user.opencck-merged-vpn`.
- В этом списке целевой туннель задается строкой `TARGET_INTERFACE='awg1'` или `TARGET_INTERFACE='awg2'`.
- В проекте подготовлены OpenWrt-скрипты:
  - `openwrt/install-vpn-switchers.sh`
  - `openwrt/check-pbr-switch.sh`

## Проверено 2026-05-26

Проверка по SSH на `root@192.168.8.1`:

- `/usr/bin/vpn1` и `/usr/bin/vpn2` установлены на роутере.
- До проверки активен был `TARGET_INTERFACE='awg2'`.
- `vpn1` отработал с `rc=0` и переложил список в `pbr_awg1_4_dst_ip_user`.
- `vpn2` отработал с `rc=0` и вернул список в `pbr_awg2_4_dst_ip_user`.
- После проверки состояние возвращено на `TARGET_INTERFACE='awg2'`.
- Таблицы маршрутизации PBR присутствуют:
  - table 257: `default via 10.8.1.8 dev awg1`;
  - table 258: `default via 10.8.1.10 dev awg2`.

## Главная цель

Сделать Cudy центральным маршрутизатором туннелируемого трафика:

- локальные клиенты LAN используют PBR и несколько VPN/proxy-выходов;
- удаленные пользователи подключаются к Cudy и обрабатываются как доверенные клиенты;
- LokVPN AI / Happ добавляется как отдельный канал, если его можно вынести из приложения;
- OpenWrt выбирает подходящий канал вручную или автоматически.

## Ветки работ

1. [BRANCH-1-remote-users.md](BRANCH-1-remote-users.md)  
   Удаленный доступ для родственников и друзей через внешний IP `195.170.35.108`.

2. [BRANCH-2-lokvpn-happ.md](BRANCH-2-lokvpn-happ.md)  
   Интеграция LokVPN AI / Happ в OpenWrt.

3. [BRANCH-3-auto-channel-selector.md](BRANCH-3-auto-channel-selector.md)  
   Таблица IP и автоматический выбор самого быстрого туннельного канала.

## Параллельная проработка

Запущены независимые рабочие ветки:

- Ветка 1: удаленные пользователи через Cudy.
- Ветка 2: LokVPN AI / Happ на OpenWrt.
- Ветка 3: автоселектор самого быстрого канала по IP.

Этот чат остается главным: результаты веток должны возвращаться сюда, после чего изменения внедряются в общую схему только после проверки рисков и бэкапа.

## Порядок внедрения

1. Стабилизировать текущую базу Cudy:
   - `vpn1`/`vpn2` установлены и проверены;
   - проверить PBR после reboot;
   - проверить cron для ежедневного обновления списка.

2. Запустить ветку 1:
   - сделать входной VPN-сервер на Cudy;
   - подключить 1 тестового удаленного клиента;
   - убедиться, что удаленный клиент проходит через те же PBR-правила, что LAN.

3. Запустить ветку 2:
   - извлечь или получить формат LokVPN/Happ-подписки;
   - выбрать реализацию на OpenWrt: WireGuard/AmneziaWG, sing-box, xray или другой клиент.

4. Запустить ветку 3:
   - сначала сделать прототип без изменения рабочей маршрутизации;
   - потом интегрировать с nft sets и PBR.

## Правило безопасности

Рабочую конфигурацию `awg1`/`awg2` не менять без бэкапа. Все новые ветки сначала внедрять как отдельные интерфейсы, отдельные firewall-зоны или отдельные скрипты, затем подключать к общей схеме.

## Следующий общий шаг

Перед внедрением новых веток снять read-only диагностику Cudy:

```sh
ip -4 addr
ip -4 route
uci show firewall | grep -Ei 'wan|redirect|rule|518|udp'
uci show network | grep -Ei 'wan|wg|awg|pbr|interface'
uci show pbr
command -v conntrack nft awk nc curl
command -v ip ping timeout flock logger
```

Эта диагностика нужна сразу для двух веток:

- ветка 1: понять, где публичный IP `195.170.35.108`, какие firewall-зоны уже есть и свободен ли порт `51830/udp`;
- ветка 3: понять, доступен ли `conntrack` и инструменты для dry-run автоселектора.

Для ветки 2 пока нужен ввод от пользователя из Happ/LokVPN: тип ссылки или протокола без секретов.

## Диагностика Cudy 2026-05-26

Read-only диагностика по SSH выполнена. Секретные ключи из `uci show network` в проект не сохранены.

Текущее тестовое состояние:

- OpenWrt: `25.12.4 r32933-4ccb782af7`, target `mediatek/filogic`, arch `aarch64_cortex-a53`.
- Cudy WAN: `eth0`, адрес `192.168.1.174/24`, default gateway `192.168.1.1`.
- Cudy LAN: `br-lan`, адрес `192.168.8.1/24`.
- `awg1`: `10.8.1.8/32`, endpoint `193.39.68.48`, PBR table 257.
- `awg2`: `10.8.1.10/32`, endpoint `45.39.33.103`, PBR table 258.
- Публичный IP `195.170.35.108` не находится на интерфейсах Cudy; Cudy стоит за upstream-роутером/NAT.
- PBR активен, `ipv6_enabled='0'`, `supported_interface='awg1' 'awg2'`.
- Активный include: `/usr/share/pbr/pbr.user.opencck-merged-vpn`.
- Package manager: `apk`; `opkg` отсутствует.
- Доступны: `nft`, `awk`, `nc`, `ip`, `ping`, `flock`, `logger`.
- Отсутствуют: `conntrack`, `curl`, `timeout`.
- Overlay: около `183.5M` свободно.
- RAM: около `368M` available.

Выводы для тестового режима:

- Для ветки 1 нужен port forward на upstream-роутере: `195.170.35.108:51830/udp -> 192.168.1.174:51830/udp`.
- Для ветки 3 dry-run через `conntrack` невозможен без установки соответствующего пакета.
- Для ветки 2 ресурсов на установку дополнительного клиента предварительно достаточно, но протокол Happ/LokVPN еще неизвестен.

## Изменения на Cudy 2026-05-26

Ветка 1:

- Был создан тестовый WireGuard `wg_in`, затем заменен на AmneziaWG `awg_in`.
- `wg_in` отключен, чтобы не конфликтовать за UDP `51830`.
- Создан входной AmneziaWG-интерфейс `awg_in`.
- `awg_in` поднят: `10.77.0.1/24`, listen port `51830/udp`, MTU `1280`.
- Создан первый тестовый peer `test-client-awg`: `10.77.0.2/32`.
- Клиентский конфиг сохранен на роутере: `/root/awg_clients/test-client-awg.conf`.
- QR-текст сохранен на роутере: `/root/awg_clients/test-client-awg.qr.txt`.
- Локальная копия для импорта: `secrets/test-client-awg.conf`, `secrets/test-client-awg.qr.txt`, `secrets/test-client-awg.png`.
- Создана firewall-зона `friends`.
- Разрешено:
  - WAN input `51830/udp`;
  - `friends -> wan`;
  - `friends -> awg1`;
  - `friends -> awg2`;
  - DNS и ping с зоны `friends` на роутер.
- Не разрешено: `friends -> lan`.
- Бэкап перед изменениями: `/root/backup-before-wg-in-20260526-120803`.
- Бэкап перед заменой на AmneziaWG: `/root/backup-before-awg-in-20260526-130042`.

Ветка 3:

- Установлены инструменты: `conntrack`, `curl`, `coreutils-timeout`.
- Проверено наличие: `conntrack`, `curl`, `timeout`, `wg`, `qrencode`.
- `conntrack -L -f ipv4` теперь показывает активные соединения LAN-клиентов.

Контроль после изменений:

- PBR-цель большого списка осталась `TARGET_INTERFACE='awg2'`.
- `pbr_awg1_4_dst_ip_user`: `0`.
- `pbr_awg2_4_dst_ip_user`: около `13173`.

## Port forward 2026-05-26

На upstream-роутере включен проброс:

```text
195.170.35.108:51830/udp -> 192.168.1.174:51830/udp
```

Это соответствует текущему тестовому WAN-адресу Cudy.

## Тест AmneziaWG-клиента 2026-05-26

После импорта `test-client-awg.conf` в AmneziaVPN сервер `awg_in` увидел клиента:

- endpoint клиента: мобильная сеть `31.173.80.36:*`;
- latest handshake был свежим на момент проверки;
- transfer шел в обе стороны;
- `conntrack` показал активные соединения с source `10.77.0.2`;
- ответы на эти соединения шли через `10.8.1.10`, то есть через текущий исходящий канал `awg2`.

Вывод: входной AmneziaWG-туннель работает, port forward работает, трафик клиента попадает в общую схему Cudy/PBR.

## Целевая домашняя схема

После завершения настройки Cudy должен стать главным домашним роутером:

- LAN Cudy: `192.168.1.1/24`.
- Текущий upstream-роутер `192.168.1.1` из тестового режима будет убран из основной схемы или переведен в bridge/ONT/AP-режим.
- Публичный вход для удаленных пользователей должен приходить на сам Cudy, а не на промежуточный роутер.

Следствия:

- В целевой схеме port forward `195.170.35.108:51830/udp -> 192.168.1.174:51830/udp` может быть не нужен, если публичный адрес или bridge/ONT напрямую отдает WAN на Cudy.
- Ветка 1 должна проектироваться под финальную LAN `192.168.1.0/24`, но тестироваться сейчас можно через временную схему `192.168.8.0/24`.
- Перед переводом Cudy в главный роутер нужно отдельно подготовить план миграции LAN, DHCP, DNS, Wi-Fi/AP и резервный доступ к LuCI/SSH.

## Правка маршрутизации удаленных клиентов 2026-05-26

После теста с телефоном выяснилось, что подключение AmneziaWG есть, но клиентские приложения не получают нормальный доступ в интернет.

Диагностика показала:

- `awg_in` получает handshake и трафик от `10.77.0.2`;
- firewall forward/NAT работают;
- часть соединений клиента уходила через `awg2`, часть через обычный `wan`.

Чтобы убрать смешанную маршрутизацию для удаленных клиентов, добавлена PBR-политика:

```text
name: Remote friends via awg2
src_addr: 10.77.0.0/24
interface: awg2
```

После reload PBR правило появилось в `pbr_prerouting`:

```text
ip saddr 10.77.0.0/24 goto pbr_mark_0x030000 comment "Remote friends via awg2"
```

Бэкап перед изменением: `/root/backup-before-friends-pbr-20260526-133603`.

## Переключатель выхода для удаленных клиентов 2026-05-26

Для тестов добавлены команды на Cudy:

```text
friends-auto
friends-wan
friends-awg1
friends-awg2
friends-route {wan|awg1|awg2} [client-ip]
```

Они меняют PBR-политику `Remote friends via ...` для `10.77.0.0/24`, перезагружают PBR и сбрасывают старые conntrack-сессии тестового клиента `10.77.0.2`.

Проверенное состояние после настройки переключателя:

```text
10.77.0.0/24 -> wan
```

Бэкап перед началом тестов маршрутов: `/root/backup-before-friends-route-test-20260526-134140`.

После проверки прямого выхода через `wan` режим возвращен к обычной PBR-логике:

```text
10.77.0.0/24 -> auto/PBR destination rules
```

В этом режиме нет принудительной source-политики для `10.77.0.0/24`: IP из активного PBR-списка идут через текущий `awg`-канал, остальные адреса идут через `wan`.

Для быстрого возврата в этот режим добавлена команда:

```text
friends-auto
```

Бэкап перед возвратом: `/root/backup-before-friends-auto-20260526-135104`.

## Исключение vseinstrumenti.ru через WAN 2026-05-26

Проблема: `https://www.vseinstrumenti.ru/` определял, что трафик идет через VPN, хотя ожидалось, что он пойдет напрямую.

Фактическая причина:

- DNS на Cudy: `www.vseinstrumenti.ru -> 185.169.155.85`;
- этот IP входит в активный PBR-set как `185.169.155.0/24`;
- значит PBR отправлял сайт через текущий `TARGET_INTERFACE='awg2'`.

Добавлено WAN-исключение:

```text
name: Force vseinstrumenti via wan
interface: wan
dest_addr: 185.169.155.0/24
```

Активное правило стоит выше большого `awg2`-set:

```text
ip daddr 185.169.155.0/24 goto pbr_mark_0x010000 comment "Force vseinstrumenti via wan"
ip daddr @pbr_awg2_4_dst_ip_user goto pbr_mark_0x030000
```

Бэкап перед изменением: `/root/backup-before-vseinstrumenti-wan-20260526-141308`.

## PBR override-списки 2026-05-26

Чтобы не править большой автообновляемый список вручную, добавлен постоянный механизм приоритетных override-списков.

На Cudy:

```text
/etc/pbr-overrides/force-wan.domains
/etc/pbr-overrides/force-wan.ips
/etc/pbr-overrides/force-vpn.domains
/etc/pbr-overrides/force-vpn.ips
```

Локальная копия в проекте:

```text
openwrt/pbr.user.opencck-merged-vpn
openwrt/pbr-overrides/
```

При каждом `/etc/init.d/pbr restart`, включая ежедневный cron в `04:17`, скрипт:

1. скачивает большой список `antifilter` + `opencck`;
2. резолвит домены из `force-wan.domains` и `force-vpn.domains`;
3. добавляет `force-vpn` в текущий `TARGET_INTERFACE`, сейчас `awg2`;
4. добавляет `force-wan` в `pbr_wan_4_dst_ip_user`.

Приоритет правил:

```text
force-wan -> wan
force-vpn -> current awg
big list -> current awg
default -> wan
```

Для `vseinstrumenti.ru` временная UCI-политика отключена, исключение теперь живет в override-файлах:

```text
force-wan.domains:
  vseinstrumenti.ru
  www.vseinstrumenti.ru

force-wan.ips:
  185.169.155.0/24
```

Проверка после отключения временной UCI-политики:

- `pbr_wan_4_dst_ip_user` содержит `185.169.155.0/24`;
- `pbr_awg2_4_dst_ip_user` тоже содержит `185.169.155.0/24`;
- правило `pbr_wan_4_dst_ip_user` стоит раньше `pbr_awg2_4_dst_ip_user`, поэтому WAN-исключение выигрывает.

Бэкапы:

```text
/root/backup-before-pbr-overrides-20260526-142329
/root/backup-before-disable-vseinstrumenti-uci-20260526-142404
```

## OpenCCK all-data признан рискованным 2026-05-28

Обнаружен вероятный мусор в большом списке: `rutube.ru` уходил через туннель, хотя должен открываться напрямую через `wan`.

Основной подозреваемый - широкий источник:

```text
https://iplist.opencck.org/ru/?format=text&data=cidr4
```

Причина: это OpenCCK all-data без `site`, `group` или `exclude`, поэтому туда могут попадать российские сервисы и их CDN. В локальном OpenWrt-артефакте `openwrt/pbr.user.opencck-merged-vpn` этот источник убран. Остаются:

```text
base: antifilter allyouneed
force-vpn: явные домены/IP/URL-источники
force-wan: явные домены/IP/URL-источники
```

Добавлены новые файлы override-источников:

```text
/etc/pbr-overrides/force-wan.urls
/etc/pbr-overrides/force-vpn.urls
```

В `force-wan.domains` добавлены:

```text
rutube.ru
www.rutube.ru
static.rutube.ru
pic.rutube.ru
cdn.rutube.ru
```

Нужно применить на Cudy после восстановления локального shell/SSH-доступа:

```text
copy openwrt/pbr.user.opencck-merged-vpn -> /usr/share/pbr/pbr.user.opencck-merged-vpn
copy openwrt/pbr-overrides/* -> /etc/pbr-overrides/
/etc/init.d/pbr restart
```

Применено на Cudy после перезапуска Codex:

```text
backup: /root/backup-before-no-opencck-20260528-080149
```

Проверка источника Rutube:

- `rutube.ru -> 109.238.90.239`, `178.248.233.148`;
- `static.rutube.ru` / `pic.rutube.ru -> 89.248.230.8`;
- `109.238.90.239` попадал в `awg2` через `109.238.88.0/22`;
- эта подсеть была найдена в `/tmp/pbr_opencck_cidr4.raw`;
- в `/tmp/pbr_allyouneed.raw` совпадения для `109.238.90.239` не было.

После применения:

```text
OPENCCK_URL / RAW_OPENCCK удалены из active script
/tmp/pbr_opencck_merged_vpn.clean: 15457 строк
/tmp/pbr_force_wan.clean: 5 строк
```

Rutube IP теперь:

```text
109.238.90.239 -> wan=yes, awg2=no
178.248.233.148 -> wan=yes, awg2=no
89.248.230.8 -> wan=yes, awg2=no
```

Состояние после применения:

- WAN ping `8.8.8.8` через `eth0`: OK;
- tunnel ping `8.8.8.8` через `awg2`: OK;
- `awg1`, `awg2`, `awg_in` подняты;
- `DC_via_Cudy` подключен к `awg_in`.

Во время `/etc/init.d/pbr restart` был короткий разрыв forwarding. Это ожидаемо для restart PBR; для дальнейших изменений лучше использовать план с предварительной проверкой и предупреждением о кратком обрыве.

## OpenAI/ChatGPT force-vpn 2026-05-28

После удаления OpenCCK all-data запросы Codex/ChatGPT с ПК `192.168.8.102` начали идти напрямую через WAN. Это было видно в `conntrack`:

```text
192.168.8.102 -> 8.6.112.0 / 8.47.69.0
reply dst=192.168.1.174
```

Добавлены в `/etc/pbr-overrides/force-vpn.domains`:

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

Добавлены текущие резолвленные IP в `/etc/pbr-overrides/force-vpn.ips` для немедленного эффекта:

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

После `/etc/init.d/pbr restart` все эти IP подтверждены в `pbr_awg2_4_dst_ip_user`.

Бэкап:

```text
/root/backup-before-openai-force-vpn-20260528-080853
```

## Ручное управление override-списками 2026-05-26

На Cudy добавлен helper:

```text
pbr-override list
pbr-override edit force-wan.domains
pbr-override edit force-wan.ips
pbr-override edit force-vpn.domains
pbr-override edit force-vpn.ips
pbr-override add wan domain example.com
pbr-override add wan ip 1.2.3.0/24
pbr-override add vpn domain example.org
pbr-override add vpn ip 8.8.8.8
pbr-override del wan domain example.com
pbr-override apply
```

`edit`, `add`, `del` и `apply` перезапускают PBR, поэтому изменения сразу попадают в nft-set.

## Индивидуальные клиенты AmneziaWG 2026-05-26

Для друзей/родственников принят принцип: один человек - один peer, один IP, один conf. Это позволяет смотреть статистику и отзывать доступ точечно.

На Cudy добавлены команды:

```text
friend-list
friend-add NAME [endpoint:port]
friend-show NAME
friend-qr NAME
friend-conf NAME
friend-revoke NAME
```

Текущий `test-client-awg` виден через `friend-list`:

```text
name            ip          endpoint              handshake  from_peer_bytes  to_peer_bytes
test-client-awg 10.77.0.2   213.87.150.114:56824  ...        ...              ...
```

Новые peer-конфиги сохраняются на Cudy:

```text
/root/awg_clients/NAME-awg.conf
/root/awg_clients/NAME-awg.qr.txt
```

`friend-revoke NAME` удаляет peer из UCI, удаляет его из live `awg_in`, переносит конфиг/QR в `/root/awg_clients/revoked/` и не затрагивает остальных клиентов.

Пояснение статистики:

- `from_peer_bytes` - байты, которые Cudy получил от клиента через `awg_in`;
- `to_peer_bytes` - байты, которые Cudy отправил клиенту через `awg_in`;
- для обычного просмотра сайтов основная загрузка друга обычно растет в `to_peer_bytes`.

Ограничение QR:

- AmneziaVPN официально не сканирует QR для `AmneziaWG native format` (`.conf`);
- поэтому основной способ передачи нашим пользователям - индивидуальный `.conf`;
- `friend-qr` оставлен только как raw WireGuard/AmneziaWG QR для клиентов, которые умеют импортировать такой формат, но для AmneziaVPN он не является надежным способом.

## Исправление friend-add 2026-05-26

При создании `friend-add DC_via_Cudy` обнаружен баг: функция поиска существующего peer перезаписывала переменную `name`, поэтому новый peer был ошибочно создан как второй `test-client-awg`.

Исправлено:

- `friendctl` больше не перезаписывает имя нового клиента при поиске существующих peer;
- новые файлы создаются в формате `NAME-awg.conf` и `NAME-awg.qr.txt`;
- ошибочно созданный peer `10.77.0.3/32` переименован в `DC_via_Cudy`;
- его файлы перенесены в:

```text
/root/awg_clients/DC_via_Cudy-awg.conf
/root/awg_clients/DC_via_Cudy-awg.qr.txt
```

Старый `test-client-awg` восстановлен как peer `10.77.0.2/32`, его конфиг восстановлен из локального `secrets/test-client-awg.conf`, QR перегенерирован на Cudy.

## WAN-RU + OpenCCK source split 2026-05-28

Active PBR source `/usr/share/pbr/pbr.user.opencck-merged-vpn` was updated and applied on Cudy.

Backup before replacement:

```text
/root/backup-before-wan-ru-opencck-20260528-090634
```

Daily PBR restart now downloads:

```text
VPN base:
- https://antifilter.download/list/allyouneed.lst
- https://iplist.opencck.org/ru/?format=text&data=cidr4

WAN-RU base:
- https://www.ipdeny.com/ipblocks/data/aggregated/ru-aggregated.zone
- https://raw.githubusercontent.com/v2fly/domain-list-community/master/data/category-ru
```

Generated files after first run:

```text
/tmp/pbr_wan_ru.clean: 8658 lines
/tmp/pbr_wan.clean: 8663 lines
/tmp/pbr_vpn_downloaded.clean: 18454 lines
/tmp/pbr_opencck_merged_vpn.clean: 18352 lines
```

Important checks:

```text
109.238.88.0/22      wan=yes vpn=no
8.6.112.0/32         wan=no  vpn=yes
8.47.69.0/32         wan=no  vpn=yes
104.18.41.241/32     wan=no  vpn=yes
185.169.155.85/32    wan=yes vpn=no
```

PBR rule order:

```text
ip daddr @pbr_wan_4_dst_ip_user  goto pbr_mark_0x010000
ip daddr @pbr_awg1_4_dst_ip_user goto pbr_mark_0x020000
ip daddr @pbr_awg2_4_dst_ip_user goto pbr_mark_0x030000
```

Real traffic from PC `192.168.8.102`:

```text
chatgpt.com -> 8.6.112.0 / 8.47.69.0 reply dst 10.8.1.10
rutube.ru -> 178.248.233.148 reply dst 192.168.1.174
vseinstrumenti.ru -> 185.169.155.85 reply dst 192.168.1.174
```

## Gemini / Google AI force-vpn 2026-05-28

Problem: `https://gemini.google.com/` reported unsupported country, even though awg2 public IPv4 was US.

Confirmed awg2 public IPv4:

```text
45.39.33.103, United States, New York, AS209854 Cyberzone S.A.
```

Main `gemini.google.com` IPv4 routes were already going through awg2, but conntrack showed Google IP `142.251.1.188` going directly via WAN:

```text
142.251.1.188 -> lb-in-f188.1e100.net
old reply dst: 192.168.1.174
```

Added to `/etc/pbr-overrides/force-vpn.domains`:

```text
gemini.google.com
bard.google.com
accounts.google.com
ogs.google.com
www.google.com
google.com
www.gstatic.com
gstatic.com
content-push.googleapis.com
generativelanguage.googleapis.com
alkalimakersuite-pa.clients6.google.com
aistudio.google.com
ai.google.dev
```

Added to `/etc/pbr-overrides/force-vpn.ips`:

```text
142.251.1.188/32
```

After PBR restart and conntrack delete, sample Google/Gemini connections from PC `192.168.8.102` route through awg2:

```text
142.251.150.2 -> reply dst 10.8.1.10
173.194.222.95 -> reply dst 10.8.1.10
64.233.162.95 -> reply dst 10.8.1.10
209.85.233.84 -> reply dst 10.8.1.10
```

## google-check helper 2026-05-28

Added `/usr/bin/google-check` on Cudy and local artifact `openwrt/google-check`.

Usage:

```text
google-check
google-check eth0 awg1 awg2
```

The helper compares each interface by:

- `ifconfig.co` public IP/geolocation;
- `ipinfo.io` public IP/geolocation;
- Google Search `html lang`;
- Gemini HTTP shell response;
- Google One plans HTTP shell response;
- recent Google/Gemini conntrack from PC `192.168.8.102`.

First run:

```text
active target: awg2
eth0: 195.170.35.108, RU, Google html_lang=ru
awg1: 193.39.68.48, ifconfig=ES, ipinfo=KZ/Almaty, Google html_lang=kk
awg2: 45.39.33.103, US, Google html_lang=en
```

## Fast vpn1/vpn2 switchers 2026-05-28

Problem: old `/usr/bin/vpn1` and `/usr/bin/vpn2` did full `service pbr restart`.
This disabled forwarding and spent a long time at:

```text
Running /usr/share/pbr/pbr.user.opencck-merged-vpn
```

Also, browser Google sessions could survive the route change through conntrack/QUIC and keep the previous region state.

Updated switchers:

```text
/usr/bin/vpn-switch
/usr/bin/vpn1
/usr/bin/vpn2
```

New behavior:

- persist `TARGET_INTERFACE` in `/usr/share/pbr/pbr.user.opencck-merged-vpn`;
- reuse cached `/tmp/pbr_opencck_merged_vpn.clean` and `/tmp/pbr_wan.clean`;
- update nft sets with a single `nft -f` batch;
- flush client conntrack for `192.168.8.0/24` and `10.77.0.0/24`;
- fall back to full PBR restart only if cached lists are missing.

Control run:

```text
vpn2: 4.41s
142.251.153.2/32 -> awg2=yes
109.238.88.0/22 -> wan=yes
Google/Gemini conntrack after switch -> reply dst 10.8.1.10
```
