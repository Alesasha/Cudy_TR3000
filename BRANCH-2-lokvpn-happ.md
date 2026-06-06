# Ветка 2. LokVPN AI / Happ на Cudy

## Цель

Добавить подписку LokVPN AI, которая сейчас работает через приложение Happ, как отдельный канал туннелирования на OpenWrt.

## Главный вопрос

Нужно понять, какой именно формат дает LokVPN/Happ:

- WireGuard;
- AmneziaWG;
- VLESS;
- Trojan;
- Shadowsocks;
- Hysteria;
- sing-box subscription;
- xray subscription;
- закрытая ссылка вида `happ://...`.

От этого зависит реализация на Cudy.

## Возможные реализации

### Если это WireGuard или AmneziaWG

Добавляем как новый интерфейс:

```text
awg3 / wg_lokvpn
```

## LokVPN direct Cudy profile 2026-06-02

Runtime Happ config was extracted from the running `xray.exe` memory after enabling Happ with:

```text
Happ AdvancedSettings:
  tun=false
  systemProxy=false
```

Extracted Xray profile:

```text
inbounds:
  socks 127.0.0.1:10808
  http  127.0.0.1:10809

outbounds:
  RU: VLESS Reality 80.90.191.117:8080
      uuid d175289d-c0bf-ed46-82e2-984fe807b1f5
      flow xtls-rprx-vision
      sni max.ru
      publicKey muasnZnj1TgX25uDi7PJgQ7ReYtDaq5z0tuoLWKJFQQ
      shortId 7ebf

  DE: VLESS Reality 80.90.191.117:8686
      uuid 21b04185-c4e6-e6d8-00d9-14604aa4c387
      flow xtls-rprx-vision
      sni max.ru
      publicKey muasnZnj1TgX25uDi7PJgQ7ReYtDaq5z0tuoLWKJFQQ
      shortId 7ebf

Happ routing final rule sends tcp,udp,quic to DE.
```

Cudy changes:

```text
local:  openwrt/lokvpn.json
router: /etc/sing-box/lokvpn.json
local:  openwrt/lokvpn-refresh
router: /usr/bin/lokvpn-refresh

old /etc/sing-box/lokvpn.json was backed up as:
  /etc/sing-box/lokvpn.json.bak-happ-pc-socks-YYYYMMDD-HHMMSS
```

Validation:

```text
sing-box check -c /tmp/lokvpn.json: OK
/etc/init.d/sing-box-lokvpn status: running
vpn-lokvpn switch: TARGET_INTERFACE='lokvpn'
curl -4 --interface lokvpn https://ifconfig.me/ip: 194.33.34.187
logread: inbound/tun[lokvpn-tun] -> outbound/vless[proxy-out] observed for client traffic
```

Current target after this run:

```text
TARGET_INTERFACE='lokvpn'
```

Дальше подключаем к PBR как еще один supported interface.

### Если это VLESS/Trojan/Shadowsocks/Hysteria

Вероятная реализация:

```text
sing-box или xray на OpenWrt
```

Дальше возможны два режима:

- proxy mode - локальный SOCKS/HTTP/TProxy;
- tun mode - отдельный tun-интерфейс, который можно подключать к маршрутизации.

### Если это только закрытая Happ-подписка

Нужно выяснить, можно ли:

- экспортировать обычные конфиги из Happ;
- получить subscription URL у провайдера;
- открыть ссылку в другом клиенте;
- использовать sing-box/xray напрямую.

Если нельзя, тогда Happ как канал на OpenWrt может быть невозможен без нештатного извлечения конфигурации.

## План работ

1. Получить от пользователя один из вариантов:
   - QR-код;
   - subscription link;
   - экспортированный config;
   - скрин/текст списка протоколов в Happ без приватных ключей.

2. Определить протокол.

3. Выбрать клиент OpenWrt:
   - WireGuard/AmneziaWG;
   - sing-box;
   - xray;
   - другой минимальный клиент.

4. Поднять канал изолированно:
   - не трогать `awg1` и `awg2`;
   - дать каналу отдельное имя;
   - проверить доступность с самого роутера.

5. Подключить к общей схеме:
   - firewall;
   - PBR или отдельные nft rules;
   - ручное переключение;
   - позже автоматический выбор в ветке 3.

## Риски

- Happ может использовать закрытую подписку, которую нельзя напрямую перенести на OpenWrt.
- На Cudy может не хватить памяти/CPU для тяжелого xray/sing-box с большими rulesets.
- TUN/proxy-интеграция сложнее, чем обычный WireGuard-интерфейс.

## Результат ветки

- Понятный ответ: можно или нельзя вынести LokVPN/Happ на Cudy.
- Если можно - рабочий канал `lokvpn` в OpenWrt.
- Инструкция по обновлению подписки.

## Первый следующий шаг

Открыть Happ и найти способ показать или экспортировать конфигурацию/подписку. Нужен формат ссылки или список протоколов, без публикации приватных ключей.

## Результат первичной проработки

По официальной документации Happ может принимать одиночные ссылки и подписки следующих типов:

- `vless://`
- `vmess://`
- `trojan://`
- `ss://`
- `socks://`
- `hy2://`
- обычный `https://` subscription URL
- зашифрованные ссылки Happ вида `happ://crypto...` / `happ://crypt5/...`

Источники:

- [Happ adding subscription](https://www.happ.su/main/faq/adding-configuration-subscription)
- [Happ share config](https://www.happ.su/main/faq/share-configuration)
- [Happ crypto link](https://www.happ.su/main/dev-docs/crypto-link)
- [Happ link examples](https://www.happ.su/main/dev-docs/examples-of-links-and-parameters)

Матрица решений:

- Если LokVPN дает WireGuard: добавить как `wg_lokvpn` / `wg3`, отдельная firewall-зона, затем PBR.
- Если LokVPN дает AmneziaWG: добавить как `awg3` / `awg_lokvpn`, но сначала проверить поддержку текущей версии AWG на OpenWrt.
- Если это VLESS/Trojan/Shadowsocks/Hysteria2/VMess: основной кандидат `sing-box` с `tun` inbound и outbound нужного типа.
- Если это Xray JSON: рассмотреть `xray-core`, но проверить актуальность версии пакета на OpenWrt.
- Если доступен только `happ://crypt5/...`: интеграция на Cudy невозможна, пока провайдер не даст обычный subscription URL или экспорт одного сервера.

Что нужно от пользователя без утечки секретов:

- префикс ссылки: например `vless://`, `happ://crypt5/`, `https://`;
- протокол, если Happ показывает его под сервером;
- если есть JSON, только имена ключей без значений;
- для WG/AWG только наличие секций `[Interface]`, `[Peer]` и имен параметров, без ключей и endpoint/token/password.

Не публиковать:

- private key;
- UUID целиком;
- password;
- full subscription URL;
- shortId;
- private endpoint tokens.

## Диагностика Cudy 2026-05-26

Для будущей установки клиента:

- OpenWrt: `25.12.4`.
- Package manager: `apk`.
- `opkg` отсутствует.
- Overlay свободно: около `183.5M`.
- RAM available: около `368M`.

Этого предварительно достаточно, чтобы рассматривать установку `sing-box`, `xray-core` или дополнительного WG/AWG-интерфейса, но решение зависит от формата подписки Happ/LokVPN.

## Подготовка к LokVPN и vpntype 2026-05-30

Приняты рабочие имена будущих выходных каналов:

```text
lokvpn
vpntype
```

Их нужно использовать как имена OpenWrt/PBR-интерфейсов независимо от протокола. Если провайдер дает WireGuard/AmneziaWG, это будут обычные UCI-интерфейсы `proto wireguard` или `proto amneziawg`. Если провайдер дает VLESS/VMess/Trojan/Shadowsocks/Hysteria2, это должны быть tun-интерфейсы от `sing-box` или `xray`, чтобы PBR видел их как маршрутизируемые выходы.

Локально добавлен безопасный классификатор:

```sh
openwrt/vpn-config-detect FILE
cat FILE | openwrt/vpn-config-detect
```

Он не печатает саму ссылку, ключи, UUID, endpoint или пароль. Выводит только тип:

```text
type=amneziawg-config
type=wireguard-config
type=vless-link
type=subscription-url
type=happ-encrypted-subscription
type=xray-or-sing-box-json
```

Текущий быстрый переключатель тоже подготовлен под новые имена:

```text
vpn1         -> awg1
vpn2         -> awg2
vpn3         -> lokvpn
vpn4         -> vpntype
vpn-lokvpn   -> lokvpn
vpn-vpntype  -> vpntype
vpn-switch INTERFACE
```

Важно: эти команды начнут работать только после того, как соответствующий интерфейс реально создан в OpenWrt и добавлен в `pbr.config.supported_interface`. До этого `vpn-lokvpn` и `vpn-vpntype` должны завершаться ошибкой, а не менять рабочую маршрутизацию.

По актуальной документации Happ на 2026-05-30:

- Happ принимает одиночные ссылки `vmess://`, `vless://`, `socks://`, `trojan://`, `ss://`;
- в примерах Happ также описан `hy2://` / Hysteria2;
- стандартная subscription-ссылка может быть обычным `https://...`;
- зашифрованная Happ-подписка начинается с `happ://crypto...` или похожего `happ://crypt4/...` / `happ://crypt5/...`;
- отдельный сервер можно расшарить из Happ как clipboard, QR или JSON, если подписка не скрытая/зашифрованная.

Источники:

- [Happ adding subscription](https://www.happ.su/main/faq/adding-configuration-subscription)
- [Happ share config](https://www.happ.su/main/faq/share-configuration)
- [Happ link examples](https://www.happ.su/main/dev-docs/examples-of-links-and-parameters)

## Следующий практический шаг

Нужно получить для каждого провайдера один из вариантов, не публикуя секреты в чат:

1. Префикс ссылки: `vless://`, `trojan://`, `ss://`, `hy2://`, `https://`, `happ://crypt...`.
2. Если Happ позволяет: `Share -> Copy to Clipboard` или `Share -> JSON` для одного сервера.
3. Если есть `.conf`: только результат `vpn-config-detect`, без содержимого файла.

Матрица внедрения:

```text
WireGuard/AWG config
  -> создать интерфейс lokvpn/vpntype
  -> добавить firewall forwarding lan/friends -> interface zone
  -> добавить pbr supported_interface
  -> проверить /root/check-pbr-switch.sh
  -> включать через vpn-lokvpn или vpn-vpntype

VLESS/VMess/Trojan/SS/Hysteria2 link or JSON
  -> поставить/настроить sing-box
  -> поднять tun interface lokvpn/vpntype
  -> добавить PBR как supported_interface
  -> проверить public IP и conntrack

Only happ://crypto or happ://crypt4 or happ://crypt5
  -> прямого подключения к Cudy нет
  -> нужен экспорт одного сервера, JSON или обычная subscription URL от провайдера
```

## Проверка документации LokVPN и VPNtype 2026-05-30

### VPNtype

Публичная инструкция VPNtype говорит не про OpenWrt, а про обычную клиентскую схему:

- получить конфиг в Telegram-боте;
- установить клиент для нужной ОС;
- нажать `+`, вставить скопированную конфигурацию и подключиться.

На странице VPNtype в качестве клиентов указаны:

- Windows / Linux: загрузки с GitHub;
- iOS / macOS: Streisand;
- Android / Android TV: v2RayTun.

Streisand в App Store описан как proxy-клиент с поддержкой `VLESS(Reality)`, `VMess`, `Trojan`, `Shadowsocks`, `Socks`, `SSH`, `Hysteria(V2)`, `TUIC`, `Wireguard`. Linux-ссылка VPNtype ведет на Hiddify, а Hiddify официально описывает себя как клиент на базе sing-box с поддержкой VLESS, VMess, Reality, TUIC, Hysteria, WireGuard, SSH и subscription/config форматов Sing-box, V2ray, Clash, Clash Meta.

Вывод для проекта: VPNtype почти наверняка выдает не классический WireGuard `.conf`, а Xray/sing-box совместимую ссылку или subscription URL. Для Cudy целевой вариант:

```text
VPNtype subscription/link
  -> локально определить тип через vpn-config-detect
  -> если это vless/vmess/trojan/ss/hy2/json/subscription
  -> установить sing-box на Cudy
  -> поднять TUN-интерфейс vpntype
  -> добавить vpntype в pbr.config.supported_interface
  -> переключать через vpn-vpntype
```

Если бот VPNtype неожиданно даст WireGuard/AmneziaWG `.conf`, тогда sing-box не нужен: создаем обычный интерфейс `vpntype` и подключаем его к PBR как `awg1`/`awg2`.

### LokVPN AI

У LokVPN публичная техническая документация по протоколам не найдена. Публично видны Telegram-канал и бот:

```text
@LOKVPN_BOT
@LOKVPN_HELP_BOT
```

В публикациях LokVPN говорится об обновлении типа шифрования, новых серверах, прокси и кнопке обновления, но не раскрывается стабильный экспортный формат. Поэтому технически LokVPN для нас классифицируется не по бренду, а по тому, что реально отдает бот или Happ:

```text
vless/vmess/trojan/ss/hy2/json/subscription
  -> sing-box TUN interface lokvpn

wireguard/amneziawg .conf
  -> native OpenWrt interface lokvpn

happ://crypto / happ://crypt4 / happ://crypt5 only
  -> напрямую на Cudy не переносится
  -> нужен share/export одного сервера, JSON или обычная subscription URL
```

### sing-box как базовый клиент для обоих провайдеров

Официальная документация sing-box подтверждает нужные нам строительные блоки:

- inbound `tun`, чтобы Cudy получил маршрутизируемый интерфейс;
- outbounds `shadowsocks`, `vmess`, `trojan`, `wireguard`, `hysteria`, `vless`, `tuic`, `hysteria2`, `selector`, `urltest`.

Для нашего проекта это лучше, чем proxy-only режим, потому что текущая схема Cudy строится вокруг PBR/nft-set и интерфейсов. SOCKS/HTTP proxy без TUN пришлось бы отдельно заворачивать через tproxy/nft, что сложнее и хуже совпадает с уже рабочим `vpn-switch`.

Источники:

- [VPNtype instruction](https://vpntype.help/%D0%B8%D0%BD%D1%81%D1%82%D1%80%D1%83%D0%BA%D1%86%D0%B8%D1%8F-vpntype/)
- [Streisand App Store](https://apps.apple.com/ru/app/streisand/id6450534064)
- [Hiddify app](https://github.com/hiddify/hiddify-app)
- [Happ adding subscription](https://www.happ.su/main/faq/adding-configuration-subscription)
- [Happ share configuration](https://www.happ.su/main/faq/share-configuration)
- [sing-box inbound](https://sing-box.sagernet.org/configuration/inbound/)
- [sing-box outbound](https://sing-box.sagernet.org/configuration/outbound/)
- [LokVPN public channel mirror](https://nicegram.app/hub/channel/lokvpn_news)

## Фактические подписки пользователя 2026-05-30

Пользователь сообщил только безопасные префиксы/начала ссылок:

```text
vpntype: https://pro.lk-server.com/subscription/...
lokvpn:  happ://crypt4/...
```

Решение:

### vpntype

Это обычная HTTPS subscription URL. Ее можно подключать к проекту через `sing-box`, если содержимое подписки является одним из поддерживаемых форматов:

```text
vless://
vmess://
trojan://
ss://
hy2:// / hysteria2://
sing-box JSON
xray/v2ray JSON
clash/mihomo YAML
```

Практический путь:

```text
1. На Cudy установить sing-box, если его еще нет.
2. Приватно скачать subscription URL на самом Cudy или локально, не сохраняя ссылку в репозитории.
3. Определить внутренний тип подписки через vpn-config-detect или отдельный приватный fetch/inspect.
4. Сгенерировать /etc/sing-box/vpntype.json.
5. Поднять TUN-интерфейс vpntype.
6. Добавить vpntype в pbr.config.supported_interface.
7. Проверить /root/check-pbr-switch.sh.
8. Переключать целевой список через vpn-vpntype.
```

### lokvpn

`happ://crypt4/...` - это закрытая Happ-ссылка. Для OpenWrt она непригодна как источник конфига, потому что серверные параметры скрыты внутри формата Happ.

Что можно сделать:

```text
1. В Happ попробовать Share / Copy to Clipboard / JSON для одного сервера.
2. В боте LokVPN запросить обычную subscription URL или экспорт для другого клиента: Hiddify, Streisand, v2RayTun, sing-box, Clash, V2Ray/Xray.
3. Если провайдер дает только happ://crypt4, канал lokvpn на Cudy пока не подключаем.
```

Итоговый приоритет внедрения:

```text
1. Сначала vpntype через sing-box TUN, потому что формат уже похож на обычную subscription URL.
2. Затем lokvpn только если удастся получить не-Happ-crypt экспорт.
```

## Экспорт из Happ и локальные файлы Happ 2026-05-30

По официальной документации Happ есть только один штатный способ получить конфиг в пригодном формате:

```text
Server List -> swipe right on server header -> share icon -> Copy to Clipboard / QR Code / JSON
```

Ограничение ключевое: Happ разрешает расшарить сервер из подписки только если подписка не encrypted и не hidden. Для encrypted subscription документация прямо говорит:

```text
server settings and subscription URL are hidden
the user cannot edit, view, or share server configurations contained within that subscription
```

`happ://crypt4/...` относится именно к encrypted/crypto link. В документации Happ `crypt4` описан как старый RSA-4096 формат, а новым вариантом назван `happ://crypt5/`. Смысл обоих одинаковый для нашего проекта: скрыть исходный subscription URL от пользователя.

Локальная проверка на Windows показала, что Happ Desktop хранит данные здесь:

```text
C:\Users\Alexander\AppData\Local\Happ\config.json
C:\Users\Alexander\AppData\Local\Happ\routing.json
C:\Users\Alexander\AppData\Local\Happ\subs.db
C:\Users\Alexander\AppData\Roaming\Happ\core
```

`subs.db` начинается с `SQLite format 3`, то есть это обычная SQLite-база. Но это не означает, что из нее корректно и безопасно можно получить `vless://`/JSON:

- официально encrypted subscription нельзя view/edit/share;
- база может хранить encrypted blob, внутренние записи Happ или уже обработанные данные без исходной ссылки;
- если серверы там и есть в расшифрованном виде, это не стабильный публичный API и обновление Happ может сломать способ извлечения;
- читать/экспортировать строки из `subs.db` нужно только локально и без публикации содержимого в репозиторий или чат.

Практический вывод по `lokvpn`:

```text
1. Сначала пробовать официальный share JSON/clipboard в Happ для одного сервера.
2. Если share отсутствует или заблокирован - запросить у LokVPN обычный subscription/export для Hiddify/Streisand/v2RayTun/sing-box/Xray/Clash.
3. Извлечение из subs.db рассматривать только как локальную диагностику: проверить наличие таблиц и типов записей, не печатая значения.
4. Не отправлять happ://crypt4 или содержимое subs.db на сторонние decrypt-сервисы.
```

Есть отдельный обходной режим, не связанный с экспортом: Happ поддерживает `Allow LAN Connections`. Он открывает на устройстве с Happ локальные HTTP/SOCKS5 proxy-порты для других устройств LAN. Это можно использовать как временный proxy-шлюз, но для нашей схемы Cudy/PBR это хуже, чем `sing-box` на роутере:

- нужен постоянно включенный ПК/телефон с Happ;
- это proxy, а не полноценный маршрутизируемый интерфейс;
- UDP/QUIC и прозрачная маршрутизация всех клиентов сложнее;
- Cudy пришлось бы дополнительно настраивать через tproxy/redsocks или задавать proxy на клиентах.

Источники:

- [Happ share configuration](https://www.happ.su/main/faq/share-configuration)
- [Happ adding configuration/subscription](https://www.happ.su/main/faq/adding-configuration-subscription)
- [Happ crypto link](https://www.happ.su/happ/dev-docs/crypto-link)
- [Happ local network connections](https://www.happ.su/main/faq/local-network-connections)

## Happ decryptor как возможный путь для lokvpn 2026-05-30

Проверен проект:

```text
https://github.com/LeeeeT/happ-decryptor
https://leeeet.dev/happ-decryptor/
```

Что он заявляет:

- browser-based decryptor для `happ://` deep links;
- поддерживает `crypt`, `crypt2`, `crypt3`, `crypt4`, `crypt5`;
- расшифровка выполняется в браузере локально;
- для `crypt1-crypt4` используются legacy RSA-PKCS1v15 blocks;
- для `crypt5` используется RSA + ChaCha20-Poly1305 и bundled key table.

Это меняет статус `lokvpn`:

```text
Было:
  happ://crypt4 напрямую непригоден для OpenWrt.

Теперь:
  happ://crypt4 напрямую все еще непригоден,
  но его можно попробовать локально расшифровать сторонним инструментом.
```

Правило безопасности:

```text
Не вставлять подписку lokvpn в публичный hosted decryptor.
Использовать только локальный запуск кода после просмотра исходников:
  git clone https://github.com/LeeeeT/happ-decryptor
  npm install
  npm run dev
```

После расшифровки результат должен быть обработан как обычная подписка:

```text
https://... subscription
  -> приватно скачать содержимое
  -> определить внутренний формат
  -> sing-box TUN interface lokvpn

vless:// / vmess:// / trojan:// / ss:// / hy2://
  -> sing-box TUN interface lokvpn

json/yaml
  -> определить sing-box/xray/clash формат
  -> конвертировать в /etc/sing-box/lokvpn.json
```

Риски:

- инструмент основан на reverse engineering, а не на официальном API Happ;
- может нарушать условия провайдера или Happ;
- может перестать работать после смены ключей/формата;
- результат расшифровки является секретом подписки и не должен попадать в репозиторий.

## LokVPN после happ-decryptor 2026-05-30

После локальной расшифровки `happ://crypt4/...` пользователь получил обычную HTTPS subscription URL:

```text
https://pf.lok-vpn.ru/?profil_id=...&pass=...
```

Полная ссылка содержит секретный `pass` и не должна сохраняться в репозитории.

Статус LokVPN меняется:

```text
Было:
  только happ://crypt4, напрямую непригодно.

Стало:
  обычная HTTPS subscription URL, можно подключать через sing-box.
```

Теперь оба новых провайдера идут одной схемой:

```text
vpntype: https://pro.lk-server.com/subscription/...
lokvpn:  https://pf.lok-vpn.ru/?profil_id=...&pass=...
```

Порядок внедрения:

```text
1. Сохранить полные subscription URL только на Cudy в приватный файл, например:
   /root/vpn-subscriptions/vpntype.url
   /root/vpn-subscriptions/lokvpn.url

2. Права:
   chmod 600 /root/vpn-subscriptions/*.url

3. Приватно скачать содержимое каждой подписки.

4. Определить внутренний формат:
   - список vless/vmess/trojan/ss/hy2 ссылок;
   - sing-box JSON;
   - xray/v2ray JSON;
   - clash/mihomo YAML;
   - другой формат.

5. Для каждого провайдера сгенерировать отдельный sing-box config:
   /etc/sing-box/vpntype.json
   /etc/sing-box/lokvpn.json

6. Поднять два TUN-интерфейса:
   vpntype
   lokvpn

7. Добавить оба в PBR supported interfaces.

8. Проверить:
   /root/check-pbr-switch.sh
   vpn-vpntype
   vpn-lokvpn
```

## VPNtype decoded subscription 2026-05-30

Подписка VPNtype была скачана и base64-декодирована локально:

```text
C:\Users\Alexander\vpn-subscriptions\vpntype.sub.txt
C:\Users\Alexander\vpn-subscriptions\vpntype.decoded.txt
```

Полные строки содержат UUID, Reality public key, short id и пароли Shadowsocks, поэтому не должны сохраняться в репозитории.

Сводка без секретов:

```text
total=14
protocols:
  vless=13
  ss=1
vless_security:
  none=1
  tls=3
  reality=9
vless_transport:
  xhttp=3
  tcp=10
```

Вывод для реализации:

```text
Первый этап:
  использовать sing-box TUN для совместимых узлов:
  - vless + reality + tcp
  - shadowsocks

Не брать в первый этап:
  vless + xhttp
```

Причина: `xhttp` - Xray-ориентированный transport. Для него нужен Xray-core или отдельная проверка поддержки конкретной версии клиента. В текущую PBR-модель проще и безопаснее сначала встроить `sing-box` с `tun` и совместимыми Reality/TCP или Shadowsocks узлами.

Локально добавлен helper для безопасной сводки подписки:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\openwrt\subscription-summary.ps1 -Path "$env:USERPROFILE\vpn-subscriptions\vpntype.decoded.txt"
```

## VPNtype sing-box config generated 2026-05-30

Создан приватный `sing-box` config:

```text
C:\Users\Alexander\vpn-subscriptions\vpntype.json
```

Он не хранится в репозитории, потому что содержит UUID, Reality public keys/short ids и Shadowsocks password.

Сводка конфига:

```text
inbounds=1
tun=vpntype
outbounds=14
final=vpntype-auto
vless=10
shadowsocks=1
urltest=1
direct=1
block=1
```

Локальные артефакты для повторения:

```text
openwrt/generate-singbox-from-subscription.ps1
openwrt/install-singbox-provider.sh
openwrt/deploy-vpntype.ps1
```

Текущий blocker: SSH из Codex к `root@192.168.8.1` отвечает:

```text
Permission denied (publickey,password).
```

Поэтому применить на Cudy можно из интерактивного PowerShell, где `ssh/scp` сможет запросить пароль:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\openwrt\deploy-vpntype.ps1
```

Что делает deploy:

```text
1. Копирует vpntype.json в /root/vpn-subscriptions/vpntype.json.
2. Копирует install-singbox-provider.sh в /root/install/.
3. Устанавливает sing-box через apk, если его нет.
4. Создает /etc/sing-box/vpntype.json.
5. Создает procd service /etc/init.d/sing-box-vpntype.
6. Создает OpenWrt interface vpntype proto none device vpntype.
7. Создает firewall zone vpntype и forwarding lan/friends -> vpntype.
8. Добавляет vpntype в pbr.config.supported_interface.
9. Перезапускает sing-box, network/firewall и PBR.
```

## VPNtype applied on Cudy 2026-05-30

Применено на Cudy через SSH/Paramiko, потому что Windows `scp` требовал SFTP, а OpenWrt не имеет `/usr/libexec/sftp-server`.

Установлено:

```text
apk add sing-box
apk add kmod-tun
/etc/sing-box/vpntype.json
/etc/init.d/sing-box-vpntype
network.vpntype proto none device vpntype
firewall zone vpntype
pbr.config.supported_interface += vpntype
/usr/bin/vpn-vpntype
/usr/bin/vpn3
```

Первый запуск выявил проблему: локальный JSON был записан PowerShell с UTF-8 BOM, и `sing-box` падал:

```text
decode config at /etc/sing-box/vpntype.json: invalid character 'ï' looking for beginning of value
```

Исправлено в `openwrt/generate-singbox-from-subscription.ps1`: JSON пишется UTF-8 без BOM.

Проверка после исправления:

```text
sing-box check -c /etc/sing-box/vpntype.json: OK
/etc/init.d/sing-box-vpntype status: running
vpntype: 172.19.0.1/30
PBR table 259 pbr_vpntype: default via 172.19.0.1 dev vpntype
```

Тест переключателя:

```text
vpn-vpntype:
  TARGET_INTERFACE='vpntype'
  pbr_vpntype_4_dst_ip_user=13132
  pbr_awg2_4_dst_ip_user=0
  ip route get 8.6.112.0 mark 0x40000 -> dev vpntype table pbr_vpntype

router curl through PBR:
  gemini.google.com -> HTTP 200
  api.openai.com -> HTTP 403 unsupported_country_region_territory
```

Вывод: канал `vpntype` технически подключен и маршрутизируется через PBR, но выбранный `sing-box urltest` outbound может быть неудачен для OpenAI. Для Google/Gemini базовая проверка прошла.

После теста активный канал возвращен на штатный `awg2`:

```text
TARGET_INTERFACE='awg2'
pbr_awg2_4_dst_ip_user=13132
pbr_vpntype_4_dst_ip_user=0
```

Ручное переключение теперь:

```sh
vpn1          # awg1
vpn2          # awg2
vpn-vpntype   # vpntype / sing-box
```

## VPNtype manual outbound selector 2026-05-30

Добавлен helper:

```text
/usr/bin/vpntype-server
openwrt/vpntype-server
```

Команды:

```sh
vpntype-server list
vpntype-server status
vpntype-server auto
vpntype-server use TAG
```

Текущие доступные теги:

```text
vless-1
ss-2
vless-3
vless-4
vless-5
vless-6
vless-7
vless-8
vless-9
vless-10
vless-11
vpntype-auto
```

`vpntype-server use TAG` меняет `route.final` в `/etc/sing-box/vpntype.json`, проверяет конфиг через `sing-box check`, перезапускает `sing-box-vpntype` и делает backup:

```text
/etc/sing-box/vpntype.json.bak-server-YYYYMMDD-HHMMSS
```

Важно: это выбирает сервер внутри канала `vpntype`. Чтобы весь PBR-список реально пошел через канал `vpntype`, отдельно нужен:

```sh
vpn-vpntype
```

Если нужно вернуться на штатный канал:

```sh
vpn2
```

## LokVPN fetch attempt after decryptor 2026-05-30

Пользователь передал расшифрованный URL через clipboard. Безопасные признаки:

```text
scheme=https
host=pf.lok-vpn.ru
path=/
query_keys=profil_id,pass
```

Попытка скачать как обычную подписку не удалась:

```text
curl with UA v2rayN/Happ/Hiddify/sing-box/curl/Mozilla:
  HTTP 404
  size=0
```

Вывод: это не прямой subscription endpoint, а промежуточная profile/deep-link ссылка Happ/LokVPN или устаревший/одноразовый URL. На текущем этапе она не подходит для генерации `lokvpn.json`.

Проверка локальной базы Happ:

```text
C:\Users\Alexander\AppData\Local\Happ\subs.db
SQLite table: subscriptions
columns: version INTEGER, data BLOB, tag TEXT, updated_at TEXT
rows: 1
data: base64-like blob, after base64 decode no text markers
```

Маркеры `vless://`, `vmess://`, `trojan://`, `ss://`, `hy2://`, `https://`, `outbounds`, `server` в `subs.db` не найдены. Значит серверные параметры не лежат там как простой sing-box/Xray config.

Проверка runtime config Happ:

```text
C:\Users\Alexander\AppData\Local\Happ\config.json
inbound: tun
outbounds:
  socks -> 127.0.0.1:10808
  direct
```

Это не серверная подписка, а локальный wrapper Happ: TUN направляет трафик в локальный SOCKS proxy Happ. Сам upstream LokVPN в этом JSON не раскрыт.

Следующие варианты:

```text
1. В Happ включить LokVPN и включить Allow LAN Connections.
   Тогда можно использовать ПК как временный SOCKS/HTTP proxy для Cudy или клиентов.

2. Включить LokVPN в Happ, затем снова проверить процессы и локальные порты:
   sing-box-tun / happd / 127.0.0.1:10808
   Возможно появится временный config/path.

3. Запросить у LokVPN экспорт для Hiddify/Streisand/v2RayTun/sing-box/Xray/Clash.

4. Reverse-engineering encrypted Happ `subs.db` не делаем без отдельного решения:
   это уже не штатный экспорт и не простой decryptor deep-link.
```

## Happ conflict and passive capture 2026-06-01

Пользователь подтвердил, что включение Happ на ПК ломает интернет из-за конфликта с Cudy. Логи Happ подтверждают причину: при connect Happ поднимает `happ-tun` и назначает DNS `1.1.1.1` на этот интерфейс. В схеме, где ПК ходит через Cudy и PBR, это конкурирует с маршрутизацией роутера.

Текущий безопасный принцип:

```text
Не нажимать Connect в Happ на ПК.
Не использовать Happ как постоянный мост для Cudy.
Дальше анализировать только passive/offline: subs.db, registry, IPC, Frida, happd logs.
```

## VPNType proxy endpoint refresh 2026-06-02

Причина сбоя `vpn-proxytr`, `vpn-proxyhk` и похожих каналов:

```text
Браузерный плагин получил новые credentials на host 185.234.59.46.
Cudy оставался на старом host 185.175.46.23.
Старые endpoint вроде 185.175.46.23:49227/49820/49710 больше не принимали TCP.
```

Дополнительно выявлено:

```text
Chrome extension storage показывал старую сетку ID: TR=76, HK=78, KZ=77, BY=80.
Edge extension storage показывал рабочую новую сетку: TR=150, HK=148, KZ=149, BY=146.
API proxy-list может отдавать старые ID первыми, поэтому простое "country -> first id" ненадежно.
```

Добавлен и применен на Cudy:

```text
/tmp/update-vpntype-proxy-runtime.sh
/usr/bin/vpntype-proxy-refresh-all
/usr/bin/proxy*-refresh обновлены
/usr/bin/vpn-proxy* обновлены
cron: */15 * * * * /usr/bin/vpntype-proxy-refresh-all >/tmp/vpntype-proxy-refresh-all.log 2>&1
```

Новая логика refresh:

```text
1. Берет proxy-list.
2. Берет известные candidate IDs по стране: новая сетка + старая сетка.
3. Для каждого candidate ID получает proxy-credentials.
4. Проверяет endpoint реальным curl -x через proxy.
5. В конфиг пишет только первый рабочий endpoint.
6. Обновляет и server, и direct route cidr endpoint/32.
7. При switch START_ON_REFRESH=1 гарантированно стартует нужный sing-box.
8. В cron refresh-all не стартует все сервисы заново, но перезапускает уже работающий или активный канал при смене endpoint.
```

Проверенные рабочие endpoint на 2026-06-02:

```text
proxytr: 185.234.59.46:49227 -> 62.60.233.69
proxykz: 185.234.59.46:49710 -> 45.136.128.172
proxyus: 185.234.59.46:49382 -> 78.153.155.125
proxyde: 185.234.59.46:49659 -> отвечает, exit IPv6
```

Проблемные на момент проверки:

```text
proxyhk: candidate 185.234.59.46:49820 и старый 185.175.46.23:49820 временно не прошли curl -x health-check
proxyby: candidate 185.234.59.46:49754 и старый 185.175.46.23:49754 временно не прошли curl -x health-check
proxynl/proxypl также не прошли health-check в этом прогоне
```

Переключатели после исправления:

```text
vpn-proxytr -> refresh id=150, switch proxytr OK
vpn-proxykz -> refresh id=149, switch proxykz OK
после проверки текущий канал возвращен на awg2
```

Проверка Cudy после сбоя:

```text
running: /usr/bin/sing-box run -c /etc/sing-box/proxytr.json
pbr_proxytr: default dev proxytr
pbr_lokvpn: unreachable default
no Happ/xray/sing-box process from PC bridge is part of Cudy routing
```

Прямой URL LokVPN снова проверен:

```text
https://pf.lok-vpn.ru/ -> HTTP 404
https://pf.lok-vpn.ru/?profil_id=946&pass=... -> HTTP 404
https://files.lok-vpn.ru/profile/geosite.dat -> HTTP 200
https://files.lok-vpn.ru/profile/geoip.dat -> HTTP 200
```

Вывод: `pf.lok-vpn.ru` не является обычным subscription endpoint. Данные геомаршрутизации доступны, но серверные параметры LokVPN через этот URL не скачиваются.

Пассивный Frida-захват:

```text
Happ.exe перезапускался без connect.
На старте Happ читает C:\Users\Alexander\AppData\Local\Happ\subs.db.
xray/sing-box не запускаются.
Первичный hook крипто-кандидатов не поймал расшифровку на старте.
90-секундный IPC monitor поймал только heartbeat в happd.
```

Следующий безопасный вариант для получения runtime config:

```text
1. Временно поставить в registry Happ:
   Preferences\AdvancedSettings\tun=false
   Preferences\AdvancedSettings\systemProxy=false

2. Перезапустить GUI Happ.

3. Запустить Frida monitor.

4. В Happ нажать только безопасное действие:
   update subscription / ping server / copy/share JSON.

5. Если нужно пробовать Connect, делать это только после tun=false и systemProxy=false,
   потому что штатный tun=true уже доказанно конфликтует с Cudy.
```
