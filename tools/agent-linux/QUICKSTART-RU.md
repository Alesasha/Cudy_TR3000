# Быстрый старт Linux Agent

Архив: `DC_via_Cudy-linux-prod.zip`.

## Установка

1. Распаковать архив в отдельную папку.
2. Открыть терминал в этой папке.
3. Выполнить:

```bash
chmod +x *.sh
./one_click_install.sh
```

Если попросит пароль `sudo`, ввести пароль Linux-пользователя.

Установщик:

- восстанавливает прямой интернет-маршрут перед началом;
- при необходимости скачивает `sing-box` в `./runtime/sing-box`;
- запускает один smoke-тест;
- ставит и запускает systemd-сервис `cudy-managed-agent.service`;
- в конце показывает `./status.sh`.

## Проверка

После установки выполнить:

```bash
./status.sh
```

Нормальные признаки:

- `control` отвечает `{"ok": true}`;
- `cudy-managed-agent.service` активен;
- в `log tail` есть `cycle applied`, `routes applied` или `probe jobs processed`;
- интернет не пропадает.

## Если что-то не работает

Отправить полный вывод:

```bash
./status.sh
```

Если интернет пропал:

```bash
sudo ./restore_direct.sh
```

Если надо полностью удалить агент:

```bash
sudo ./uninstall_systemd.sh
```

## Без скачивания sing-box

Если скачивать бинарники нельзя, заранее положить `sing-box` в `runtime/sing-box` и запустить:

```bash
AUTO_INSTALL_SINGBOX=0 ./one_click_install.sh
```
