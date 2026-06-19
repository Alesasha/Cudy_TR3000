# Инструкция для Димы: Linux Agent Test

1. Распаковать архив `DC_via_Cudy-linux-prod.zip`.

2. Открыть терминал в распакованной папке и выполнить:

```bash
chmod +x *.sh
RUN_ONCE=1 ./managed_agent.sh
./test_prod_agent.sh
```

3. Если попросит пароль `sudo`, ввести пароль Linux-пользователя.

4. Если в выводе будет ошибка `sing-box not found`, остановиться и прислать этот вывод.

5. Если тест прошёл, можно включить автозапуск:

```bash
sudo ./install_systemd.sh
```

Проверка после автозапуска:

```bash
tail -f managed-agent.log
```

Откат, если интернет сломался:

```bash
sudo systemctl disable --now cudy-managed-agent.service
sudo ./restore_direct.sh
```
