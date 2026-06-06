# Cudy TR3000 VPN Routing Hub

Проект управляет Cudy TR3000/OpenWrt как центральным маршрутизатором VPN/proxy-выходов.

Текущая цель: дать пользователю локальный веб-интерфейс для выбора выхода по доменам, включая режим `Auto`, а администратору - полный контроль над серверами, пользователями, профилями провайдеров и правилами маршрутизации.

## Что Уже Есть

- Собственные AmneziaWG-выходы:
  - `awg1`: Megahost Aktau;
  - `awg2`: HostVDS US West.
- Удаленные пользователи через входящий AmneziaWG на Cudy.
- VPNtype и LokVPN как sing-box/TUN каналы.
- PBR override-списки для принудительного WAN/VPN.
- CLI для создания клиентов и статистики: `tools/awg_client_add.py`.
- Stage 1 inventory: `config/vpn_inventory.json` и `tools/vpn_inventory.py`.
- Локальная web-панель MVP: `tools/vpn_control_app.py`.

## Быстрый Старт Для Разработчика

```powershell
python -m pip install -r requirements.txt
```

```powershell
python tools\vpn_inventory.py validate
python tools\vpn_inventory.py list
python tools\vpn_inventory.py admin-list --include-disabled
python tools\vpn_control_app.py init-db
python tools\vpn_control_app.py create-user admin --role admin
python -m py_compile tools\vpn_inventory.py tools\awg_client_add.py
```

Для live-снимка Cudy:

```powershell
$env:CUDY_SSH_PASSWORD = '<router password>'
python tools\vpn_inventory.py refresh-cudy
Remove-Item Env:CUDY_SSH_PASSWORD
```

## Структура

- `config/` - статический каталог серверов и будущие конфиги приложения.
- `tools/` - локальные Python-утилиты оператора.
- `openwrt/` - скрипты и артефакты для Cudy/OpenWrt.
- `docs/` - документация для разработчика и оператора.
- `secrets/` - локальные ключи, клиентские профили и QR; игнорируется Git.

## Документация

- [Architecture](docs/architecture.md)
- [Inventory](docs/inventory.md)
- [Operations](docs/operations.md)
- [Local control app](docs/control-app.md)
- [Security](docs/security.md)
- [GitHub publishing](docs/github.md)

Исторические рабочие заметки:

- [MAIN.md](MAIN.md)
- [BRANCH-1-remote-users.md](BRANCH-1-remote-users.md)
- [BRANCH-2-lokvpn-happ.md](BRANCH-2-lokvpn-happ.md)
- [BRANCH-3-auto-channel-selector.md](BRANCH-3-auto-channel-selector.md)

Часть старых заметок может быть в неверной кодировке. Актуальная документация находится в `docs/`.
