# Node Feed Control Plane

Публичный репозиторий для системы обновления DNS/policy feed-ов через центральный узел `hub`.

Цель репозитория:
- хранить код и обезличенные контракты системы;
- не хранить реальную топологию, inventory и production-данные;
- позволять поднять `hub`-контур и клиентские роутеры по шаблонам;
- отражать текущую рабочую модель, где `fd4/fs4`, `foreign_active`, `corp_active` и `admin_active` уже живые.

Что входит:
- `builder/` — код генерации runtime-бандлов и manifest;
- `node/` — клиентские скрипты site-router и OpenWrt runbook;
- `hub/` — серверные скрипты контрольной ноды;
- `schemas/` — JSON Schema для manifest, observed и registry;
- `seeds/` — примеры публичных seed/source-конфигов.

Что не входит:
- реальные hostnames, IP, порты, inventory;
- production release bundles;
- observed history, candidate data, approved private delta;
- токены, ключи, PSK, SSH config;
- реальные operational specs.
- operational docs и runbooks поддерживаются отдельно и не коммитятся в этот public skeleton.

## Текущее состояние

Публичный skeleton соответствует current live layout:
- site-router-ы работают в IPv4-only режиме;
- `dnsmasq` пишет в `fd4`, static feed loader пишет в `fs4`;
- policy routing использует `foreign_active`, `corp_active`, `admin_active`;
- `awgde`, `awgpl`, `awgru` - текущие target tunnel names;
- `hub` принимает observed batches, строит candidate outputs и публикует versioned runtime bundles;
- site-router-ы делают pull с `hub` и остаются на последнем `current`, если обновление не удалось.

## Модель

Система делится на три контура:

1. `critical runtime`
- маленький боевой профиль для роутеров;
- качается только с `hub`;
- применяется атомарно на роутере;
- использует два runtime-слоя:
  - `foreign_dns_v4` для доменной классификации через `dnsmasq nftset`
  - `foreign_static_v4` для curated static IP/CIDR layer

Короткие release file names:
- `crit.domains`
- `dnsmasq-fd4.conf`
- `nft-fs4.txt` from curated subnet feeds
- `ref.domains`
- `manifest.txt`

Runtime-алиасы:
- `foreign_dns_v4` -> `fd4`
- `foreign_static_v4` -> `fs4`

Текущая модель deliberately IPv4-only:
- foreign runtime не строится для IPv6;
- туннели и policy layer считаются IPv4-only;
- любая future IPv6-поддержка должна вводиться как отдельное расширение, а не как скрытый недоделанный слой.

2. `reference corpus`
- полный справочный корпус доменов/IP;
- используется для suffix-match и анализа покрытия;
- не подключается напрямую в runtime роутера.

3. `observed -> candidate -> approved`
- живёт на `hub`;
- строится из агрегированной DNS-истории роутеров;
- штатно auto-promote по правилам;
- `deny` = hard reject, `noise` = exception queue, thresholds определяют, что вообще может стать candidate;
- manual handling нужен только для exceptions;
- не коммитится в публичный git.

4. `observability`
- site-router-ы пушат `node-health` и observed batches на `hub`; `hub` не SSH-поллит роутеры в `hybrid` режиме;
- `hub` и `egress-vps` наблюдаются через hub-side SSH polling/self-checks;
- `node-agent` остаётся будущим read-only HTTP/API слоем, если позже решим открыть его на самих узлах;
- Prometheus хранит только low-cardinality health/routing/feed metrics;
- DNS/client history и route/feed events хранятся отдельно в hub event store: SQLite first, ClickHouse later only if event volume grows;
- `telemetry-dashboard.py` уже поднят как read-only host service на hub `<vpn-bind-ip>:19090` поверх push ingestion, SQLite snapshot store и `/metrics`;
- `hub-dashboard` показывает topology, health, active egress, Resolver cards, tunnel state and client/domain inference, grouped by role (`site-router`, `hub`, `egress-vps`).

## Cadence

- `GitHub -> hub`: раз в сутки;
- `hub -> upstream sources`: раз в сутки ночью;
- `site-router -> hub`: раз в сутки, плюс boot-time check;
- observed upload: раз в сутки после локальной агрегации.

Node update contract:
- boot check всегда выполняется;
- daily poll идет с jitter;
- manual refresh допускается;
- при ошибке применяется backoff;
- невалидный release не активируется.

## Размещение данных

Public repo:
- код;
- схемы и шаблоны;
- обезличенные docs;
- публичные source/seed профили.

Hub bind override:
- public template lives at `hub/examples/hub.env.example`;
- real VPS value lives in `/etc/node-control/hub.env`;
- the bind address itself is host-local state and is not committed.

`hub` runtime:
- observations;
- candidate;
- approved private delta;
- release storage;
- node registry;
- secrets.

## Следующие шаги

1. Утвердить структуру каталогов и контракты файлов.
2. Поддерживать `critical` profile и curated service IP layer в актуальном виде.
3. Держать shell-скрипты `node/` и `hub/` синхронизированными с live runtime.
4. Переносить builder logic и policy updates только через версионированные изменения.
5. Вести operational DNS/update runbook отдельно от public repo.

## Минимальный запуск hub

1. Собрать контейнер `hub` через `hub/docker-compose.yml`.
2. Контейнер сам выполнит bootstrap:
   - соберёт runtime bundle из публичных источников;
   - опубликует его в `runtime/public/node-feeds`;
   - поднимет HTTP сервер на `:18080`.
3. На site-router указать:
   - `FEED_BASE_URL=http://hub:18080/node-feeds`
   - `FEED_PROFILE=critical`
4. Запустить node update flow и проверить, что он скачивает:
   - `manifest.txt`
   - `manifest.txt.sha256`
   - `releases/<version>/*`
