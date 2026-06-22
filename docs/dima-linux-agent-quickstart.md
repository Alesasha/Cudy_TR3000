# Инструкция для Димы: Linux Agent

1. Распаковать архив `DC_via_Cudy-linux-prod.zip`.

2. Открыть терминал в распакованной папке и выполнить:

```bash
chmod +x *.sh
./one_click_install.sh
```

Если попросит пароль `sudo`, ввести пароль Linux-пользователя.

Если `sing-box` не установлен и не лежит в `./runtime/sing-box`, установщик
попробует скачать его автоматически. Если скачивание запрещено, надо заранее
положить бинарник в `runtime/sing-box` или запустить:

```bash
AUTO_INSTALL_SINGBOX=0 ./one_click_install.sh
```

3. Проверить состояние:

```bash
./status.sh
```

Нормальные признаки:

- `control` отвечает `{"ok": true}`;
- в `managed transports` есть только реально нужные транспорты;
- в конце лога есть строка `cycle applied`.

Если интернет сломался или надо полностью откатить агент:

```bash
sudo ./uninstall_systemd.sh
```

Если надо только вернуть прямой маршрут без удаления сервиса:

```bash
sudo ./restore_direct.sh
```
