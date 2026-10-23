# pyos-packages

Репозиторий пакетов для pyOS и инструмент для сборки `index.json`.

Пакеты — это готовые `.pkg.json` формата 1. Генератор не собирает пакеты,
не распаковывает их и не выполняет код из них. Он только читает метаданные
и формирует индекс, который использует pyOS.

## Layout

```text
pyos-packages/
  index.json
  packages/
    hello-1.0.0-1-any.pkg.json
    libc-1.0.0-1-any.pkg.json
    coreutils-1.2.0-1-any.pkg.json
  tools/
    gen_repo_index.py
  tests/
    test_gen_repo_index.py
```

## Добавление пакета

1. Соберите или подготовьте `.pkg.json` формата 1.
2. Положите файл в `packages/`.
3. Убедитесь, что файл лежит непосредственно в `packages/`, без вложенных
   каталогов.
4. Перегенерируйте `index.json` вручную или дождитесь GitHub Action после
   push: при изменении `packages/**` индекс обновляется автоматически.

Имя файла рекомендуется делать в виде
`<name>-<version>-<release>-<arch>.pkg.json`, например
`hello-1.0.0-1-any.pkg.json`.

В `packages/` не должно быть двух файлов с одинаковым набором
`(name, version, release, arch)`. Генератор считает это ошибкой.

### Текстовые и бинарные файлы

`packages/*.pkg.json` может содержать как текстовые, так и бинарные файлы.
Для каждого элемента `files[]` поле `encoding` задаёт формат `content`:

- `"utf-8"` / `"utf8"` (по умолчанию) — обычный текст;
- `"base64"` — ASCII-строка с base64-представлением точных bytes.

`pkg pack` в pyOS выбирает кодировку автоматически: валидный UTF-8 пишется
как текст, остальное — как base64. Старые пакеты без `encoding` остаются
совместимыми.

Генератор индекса не декодирует `content` и не проверяет кодировку: ему
достаточно метаданных пакета. Поэтому для base64-файлов изменения в
`tools/gen_repo_index.py` не требуются.

## Генерация индекса

`index.json` перегенерируется автоматически GitHub Action
(`.github/workflows/repo-index.yml`) при push в `main`, если изменены файлы
в `packages/**` или `tools/gen_repo_index.py`. Action
запускает генератор, и если `index.json` изменился, коммитит его с сообщением
`chore: rebuild repo index` и пушит обратно в `main`. Сам `index.json` не
входит в paths триггера, поэтому цикл не возникает.

Ручной запуск по-прежнему поддерживается. Из корня репозитория:

```sh
python tools/gen_repo_index.py
```

Параметры:

```text
python tools/gen_repo_index.py [--repo NAME] [--packages DIR] [--out FILE]
```

Значения по умолчанию:

- `--repo core`
- `--packages ./packages`
- `--out ./index.json`

Скрипт использует только стандартную библиотеку Python 3.11 и не импортирует
`pyos`.

Подкаталоги внутри `packages/` не обходятся: генератор печатает warning в
stderr и пропускает их. Ошибки чтения, JSON, формата, обязательных полей и
дубликатов также печатаются в stderr, код выхода ненулевой.

`index.json` записывается атомарно: сначала во временный файл в том же
каталоге, затем через `os.replace`.

## Формат index.json

```json
{
  "format": 1,
  "repo": "core",
  "generated": "2026-10-23T22:40:00Z",
  "packages": [
    {
      "name": "hello",
      "version": "1.0.0",
      "release": 1,
      "arch": "any",
      "filename": "hello-1.0.0-1-any.pkg.json",
      "url": "packages/hello-1.0.0-1-any.pkg.json",
      "sha256": "...",
      "size": 1234,
      "depends": [],
      "provides": [],
      "conflicts": [],
      "description": "Hello program",
      "license": "MIT",
      "signature": null
    }
  ]
}
```

`sha256` и `size` считаются по исходным байтам файла на диске, а не по
пересериализованному JSON. `generated` — время в UTC в формате ISO 8601 с
суффиксом `Z`.

Пакеты сортируются по имени по возрастанию, затем по версии через
`pkgvercmp` по убыванию, затем по `release` по убыванию, затем по `arch` и
`filename` по возрастанию. Поддерживаются epoch и числовые сегменты версии:
`1.9 < 1.10`, `1.0 < 1.0.1`, `1:1.0 < 2:0.1`.

## Ограничения

- `.pkg.json` поддерживает файлы в UTF-8 и base64 (`files[].encoding`).
  `tar`/`zip`-архивы, scriptlets и hooks не поддерживаются.
- `sha256` и `size` считаются по исходным байтам файла `.pkg.json`;
  генератор не декодирует base64 внутри пакета.
- `signature` на этом этапе всегда `null`; подписи не реализованы.
- GitHub Action автоматически перегенерирует `index.json` при push в
  `packages/**` или `tools/gen_repo_index.py`; ручной запуск также доступен.

## Ссылки

- [README pyOS](../code/pyos/README.md)
