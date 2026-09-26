# Gelen A2A ve çok kullanıcılı erişim için güvenlik tasarımı
**Durum:** Tasarım önerisi; bu belge kodu, endpoint'i veya veri şemasını etkinleştirmez.
**Kapsam:** Issue #28. Mevcut API anonimdir ve tek sunucu yöneticisi/Tailscale güven sınırına dayanır.
Bu doküman uygulanacak kontrol modelini ve küçük uygulama dilimlerini tanımlar. Üretim dağıtımından önce tehdit modellemesi ve güvenlik incelemesi gerekir.

## 1. Bugünkü sistem ve sınır
create_app içindeki FastAPI endpoint'leri doğrudan çalışma zamanı/DB katmanına bağlanır; kimlik doğrulama, kullanıcıya göre sorgu filtresi veya sahiplik denetimi yoktur. /, /health, /agents, /conversations, /rooms, /chat/..., /runs/{id}, /a2a/targets, /mcp/tools ve /codex/models anonim erişilebilir.
SQLite/SQLAlchemy tabloları agents, agent_capabilities, conversations, conversation_members, messages, runs, run_events, human_chat_sessions/messages/runs/events, tasks, rooms, room_participants, room_runs/turns/events ve queue_jobs kayıtlarını içerir. Sahiplik sütunu yoktur; ConversationMember ajan üyeliğidir, kullanıcı üyeliği değildir.
POST /agents hem ajan oluşturup yönetici yapılandırması gibi davranır; PATCH /agents/{id} ajanı değiştirebilir. Giden A2A yalnızca yöneticinin AGENT_RUNTIME_A2A_TARGETS kataloğunu kullanır ve henüz gelen A2A sunucusu yoktur (docs/a2a.md). Tek kalıcı worker DB kuyruğunu işler. DB başlangıcında SQLite ALTER TABLE uyumluluk adımları vardır; migration framework bulunmaz.
**Mevcut ve gelecek sınırı:** Bu PR yalnızca tasarım dokümanı ve README yol haritasıdır. Yeni endpoint, kimlik middleware'i, sahiplik sütunu/migrasyonu, token, CORS veya servis değişikliği bu PR'ın parçası değildir. Şu anki uygulama güvenli şekilde çok kullanıcıya açılamaz; Tailscale ağ erişimi uygulama seviyesinde kimlik doğrulama yerine geçmez.

## 2. Tehdit modeli ve güven sınırları
Korunacak varlıklar: ajan talimat/model/araç yetkileri ve katalog yapılandırması; kullanıcı konuşma/oda/mesaj/görev/run/event içerikleri; Codex/provider kimlik bilgileri ve A2A token'ları; kuyruk bütünlüğü, maliyet ve kullanılabilirlik; denetim kayıtları.
Aktörler: kimliği doğrulanmamış istemci; oturum açmış kullanıcı; yönetici; kimliği doğrulanmış A2A makine istemcisi; uygulama API'si ve ayrı worker; model/ajan çıktısı; yapılandırılmış uzak A2A hedefi; DB/host operatörü.
Ana tehditler: IDOR ve çapraz kiracı okuma/yazma; kullanıcının ajan, model, araç ya da uzak hedef yetkisini artırması; CSRF/XSS/oturum çalma; çalınan/geniş kapsamlı bearer token ve tekrar; kötü niyetli Agent Card veya A2A mesajıyla SSRF; sahte istemci kimliği; prompt enjeksiyonu; aşırı görev/worker/kota tüketimi; içerik ve sırların log/trace yanıtlarına sızması; yanlış tenant'a event/polling yanıtı.
Güven varsayımları: TLS anahtarları, OIDC issuer ve imzalama anahtarları, sunucu/worker host'u ve DB operatörü güvenilir. Tenant ID, e-posta, agent id veya A2A messageId istemciden geldi diye güvenilir kimlik sayılmaz. Ajan/model içeriği hiçbir zaman yetki kararı değildir. Uzak A2A cevapları güvenilmeyen veri kabul edilir.
Güven sınırları: Browser ↔ HTTPS/API; OIDC sağlayıcı ↔ callback; API ↔ DB; API ↔ kuyruk worker; M2M istemci ↔ A2A listener; uygulama ↔ yapılandırılmış uzak A2A origin'i. Kuyruk mesajlarıyla worker'a taşınan principal ve tenant bağlamı sunucu tarafından yazılmış kalıcı kimlik olmalıdır.
### Kötüye kullanım senaryoları ve temel kontroller
- Kullanıcı başka birinin conversation_id veya run_id değerini tahmin eder: tüm kaynak sorguları tenant ve sahiplik filtresiyle; hassas kaynaklarda 404.
- Normal kullanıcı agent tool listesi ya da enabled alanını değiştirir: public kullanıcı yalnızca kendi çalışma alanı ajanlarını yönetir; global katalog/sağlayıcı/MCP/A2A konfigürasyonu admin/service-account işlemidir.
- Cross-site sayfa oturum cookie'siyle mesaj kuyruğa ekler: CSRF token + Origin kontrolü + SameSite cookie.
- A2A token'ı başka tenant veya method için kullanılır: issuer/subject, audience, tenant, scopes ve exp/nbf doğrulanır; method başına scope; varsayılan deny.
- Uzaktaki Card localhost/metadata IP'ye yönlendirir: gelen A2A Card URL kabul etmez; giden hedefte allowlist ve DNS rebinding kontrolleri korunur. Redirect, private/link-local/reserved IP kaçışı engellenir.
- Agent Card veya yanıtı komut/URL gibi veri taşır: yalnız sabit endpoint ve A2A HTTP+JSON 1.0 whitelist; Card public alanları sınırlandırılır; remote metin güvenilmez içeriktir.
- Saldırgan tekrar tekrar SendMessage ile maliyeti yükseltir: principal ve tenant bazında rate/concurrency/token/turn kotaları, gövde boyutu, queue limiti, deadline ve idempotency.
- Hata/olay yanıtı prompt, token ya da kişisel veri sızdırır: allowlist audit alanları, içerik redaksiyonu; sırlar log/header/trace'e yazılmaz.

## 3. Kimlik türleri ve yetki kararı
Her istekte doğrulanmış principal nesnesi oluştur: {kind, subject, tenant_id, scopes, authn, credential_id}. DB sorgusu principal tenant'ına göre scope edilir; body içindeki owner_id, tenant_id, sender agent veya principal alanı reddedilir. Kimlik yoksa 401; bilinen ama kapsamı olmayan eylem 403; başka tenant kaynağı 404; aynı tenant'ta görünür fakat yasaklı işlem 403. HTTP 404'ün varlığı gizlemek için kullanılmasına RFC 9110 izin verir.
**Human user:** OIDC iss+sub birleşimiyle kalıcı ID; e-posta değişebilir, kimlik anahtarı değildir. Workspace/tenant membership ve rol server DB'sinden alınır.
**Tenant admin:** yönetim API'sinde açık rol. İnsan oturumundan güçlü kimlik doğrulama ve ayrı audit; provider secret, MCP/A2A katalogları ve global ayarları sadece admin/service account yönetir.
**Service account:** worker veya yönetim otomasyonu için ayrı issuer subject/credential; browser cookie kabul etmez. Worker kullanıcı adına işlem yapmaz; queued request'ten server-stamped tenant/principal taşır.
**A2A client principal:** opaque client ID + tenant + grant kayıtlı, döndürülebilir secret/token. İstemci adı/messageId kimlik sayılmaz. OAuth client credentials gelecekte tercih edilen issuance olabilir; ilk M2M dilimi kısa ömürlü/rotatable opaque bearer da kullanabilir; token hash'i saklanır ve revoke kontrolü olur.
**Ajan:** principal değildir. Yerel agent ID, A2A caller kimliği, konuşma sahibi ya da admin rolü elde etmez. Agent-to-agent mesajı yalnız kullanıcı/sistem principal'ının önceden verdiği izinle yürür.
Scope örnekleri: agents:read/write, conversations:read/write, rooms:read/write, runs:read, a2a:card:read, a2a:message:send, a2a:task:read. Scope tenant sınırını geçemez. Role ve ownership denetimi handler ve DB filtreli fetch'te uygulanır.

## 4. Browser kimlik doğrulaması: OIDC + PKCE, server session
Browser OIDC Authorization Code akışı kullanır; PKCE S256, tam eşleşen redirect URI, state ve nonce, issuer/audience/imza/expiry doğrulaması gerekir. RFC 9700'ü takip et; implicit/password grant yok. OAuth erişim/ID token'ı JavaScript'e verme ve localStorage/sessionStorage içinde tutma.
Callback authorization code'u backend'de takas eder. Uygulama opak, kriptografik rastgele session ID üretir; yalnız hash'i server-side session store'da OIDC iss/sub, tenant, rol, oluşturma/son erişim, idle/absolute expiry ve session version tutulur. Tarayıcı cookie'si Secure; HttpOnly; SameSite=Lax; Path=/ olmalı; Domain ve browser'da kalıcı refresh-token yok. Logout session'ı revoke eder ve cookie'yi expire eder; admin global session revoke sağlar.
Cookie otomatik gönderildiği için tüm state-changing metotlara CSRF önlemi: synchronizer token (session'a bağlı, custom X-CSRF-Token header); Origin exact allowlist; Sec-Fetch-Site ek sinyal. SameSite=Lax tek başına yeterli değildir. GET/HEAD/OPTIONS yan etkisiz olmalı. UI ayrı origin ise sabit origin + credentials CORS gerekir; mümkünse aynı origin reverse proxy.
Session fixation için OIDC dönüşünde session ID rotate et; state tek kullanımlık kısa süreli; login/logout/callback'te cache-control no-store ve Referrer-Policy no-referrer. Idle ve mutlak ömür deployment politikasında konfigüre edilsin; admin için yeniden doğrulama/MFA. XSS'e karşı CSP ve output escaping; HttpOnly XSS'in yetkili işlem yapmasını tek başına engellemez.
OIDC issuer/audience/redirect allowlist deployment yöneticisi tarafından yapılandırılır; kullanıcı kontrollü issuer/discovery URL SSRF ve açık yönlendirme doğurur. Sağlayıcı seçimi taşınabilir OIDC standardı olmalı, tek vendor'a bağlanmamalı.

## 5. M2M bearer güvenliği
Bearer yalnız TLS üzerinden Authorization: Bearer header'ında taşınır; query, URL, body, Agent Card veya cookie yok. Uzun ömürlü paylaşılan secret yerine client başına credential, en az yetki scopes, sabit tenant ve mümkünse audience kullan. Token tahmin edilemez; DB'de token hash'i ve credential metadata saklanır. Secret bir kez gösterilir, loglardan maskelenir.
Rotasyon overlap penceresi ve anlık revoke: iki credential geçici kabul edilebilir, eski idempotency verisini etkilemez. Credential disable/revoke cache TTL'si belgelenir; deploy sonrası revoke gecikmesi test edilir. İşletim olgunlaşınca OAuth 2.0 client credentials veya mTLS/sender-constrained erişim değerlendirilebilir; A2A Card'ın security scheme'i gerçek server policy ile aynı olmalı.
Her A2A client için tenant_id, allowed agent/catalog seti, maximum concurrency, rate/quota, scopes ve expiry açıkça konfigüre edilir. Başka tenant conversation ID kullanımı 404; tenant içindeki scope ihlali 403. Token içindeki client ID, kullanıcı id'siyle karıştırılmaz. Browser oturumu A2A bearer kabul etmez; bearer API'nin insan sohbetini taklit edemez.

## 6. Kaynak sahipliği ve endpoint matrisi
Migration öncesinde canlı DB yedeği, tablo/row sayımı, snapshot checksum, geri yükleme denemesi ve read-only dry run şarttır. Önce tek mevcut operatör için sabit legacy tenant ve owner_subject oluştur; eski ajanlar/konuşmalar/odalar aynı legacy tenant'a bağlanır. Eski konuşma katılımcılarını sahibi sayma. Aktif kullanıcı daveti olmadan veriyi diğer tenant'a taşıma.
Backfill tamamlanmadan yeni auth kodu açılmaz; kolonlar nullable eklenir, backfill/doğrulanır, sonra NOT NULL/index/FK olur. SQLite için DB'yi durduran transaction/backup'lı rebuild migration planı; create_all/ad hoc ALTER ile tenant migration yapılmaz. Dual-read/legacy bypass kaldırma release gate olmalı.
| Kaynak ve route grubu | User | Tenant admin | M2M A2A | Sahiplik ve hata |
|---|---|---|---|---|
| GET / | UI sign-in | aynı | deny | UI auth challenge |
| GET /health | public minimal veya internal-only | aynı | same | içerik sızdırmaz |
| GET /agents | tenant safe catalog | tenant catalog | deny | agent config filtresi |
| GET /codex/models | admin only | allow | deny | provider/model metadata |
| GET /mcp/tools | admin only | allow | deny | yönetici MCP catalog |
| GET /a2a/targets | tenant safe subset | allow | deny | URL/credentials never returned |
| POST /agents | deny (admin-managed catalog) | create | deny | tenant server principal'dan |
| PATCH /agents/{id} | deny | update | deny | cross-tenant 404 |
| POST /conversations | own agents | allow | deny | tenant + owner principal |
| GET /conversations/{id} | owner/member policy | audited support | deny | foreign 404 |
| POST /chat/conversations | own agent | allow | deny | conversation sahibi |
| POST /chat/conversations/{id}/messages | owner only | audited support | deny | parent owner join |
| POST /conversations/{id}/messages | owner/authorized agent | admin support | deny | sender agent body ile principal olmaz |
| POST /conversations/{conversation_id}/messages/async | owner/authorized agent | admin support | deny | queued principal stamped |
| POST /chat/conversations/{id}/messages/async | owner only | admin support | deny | queued principal stamped |
| POST /rooms | own agents | allow | deny | room tenant + owner |
| GET /rooms/{id} | owner | audited support | deny | foreign 404 |
| POST /rooms/{id}/runs | owner | support audited | deny | room parent filter |
| GET /rooms/{id}/runs | owner | support audited | deny | room parent filter |
| GET /runs/{id} | run parent owner | support audited | deny | all run types filtered |
| messages/tasks/events | parent owner | support audited | deny | no direct unscoped lookup |
| GET /.well-known/agent-card.json | safe public Card | Card config | safe public Card | no private agents/secrets |
| POST /message:send | deny | explicit tooling scope | a2a:message:send | client+tenant task owner |
| GET /tasks/{id} | deny | task read scope | a2a:task:read | mismatched task 404 |
API path listesi koddan türetilmiştir; implementation öncesi route inventory testinden geçir. Parent kaynak sorguları SQL seviyesinde tenant/owner ile daraltılmalı; önce global get edip sonra response filtrelemek kabul edilmez. Parent-child ilişki aynı tenant transaction içinde doğrulanır. Admin support erişimi sebep kodu ve audit ister; normal tenant-admin başka tenant içeriğini göremez.
Önerilen ilk policy: tenant içi paylaşılan agent kataloğu, agent oluşturma ve talimat/araç/model düzenleme tenant admin'inde; normal kullanıcı yalnız admin'in yayımladığı tenant ajanlarını çalıştırır. Böylece kullanıcı gönderdiği agent ID veya capabilities ile sahiplik/araç yetkisi kazanmaz. Admin ayarlarından verilen araç ve model yetkisi kullanıcı tarafından genişletilemez.
Migration'da tenant parent'tan belirlenebilir: message→conversation; run→conversation; human chat session→conversation; tasks→human chat run/conversation; events→run; room participant→room; room run/turn/event→room/run; queue job→run parent. Agent global mi tenant-scoped mı kararı verilmeden FK/unique index yapılmamalı. Composite index tenant_id ile başlamalı.

## 7. Gelen A2A v1 minimal profil
A2A 1.0 HTTP+JSON binding'i hedefle; implementasyondan önce resmi spec ve server conformance fixtures ile exact wire format / media type doğrula. Her istek A2A-Version: 1.0 taşımalı; SendMessage JSON gövdesi ve application/a2a+json media type, GetTask ise spec'in HTTP+JSON GET/query binding'ine uygun olmalı. HTTP hataları application/a2a+json içindeki google.rpc.Status ProtoJSON biçimini ve A2A ErrorInfo ayrıntılarını kullanmalı. Agent Card securitySchemes/securityRequirements gerçek Bearer doğrulamasıyla eşleşmeli. Bu servis A2A Server, uzağı arayan remote A2A Client olacaktır. İlk profil:
- GET /.well-known/agent-card.json: stable service identity, version, public skills, HTTP+JSON 1.0 interface ve gerçek authentication requirement. Global Card shared service capability sunar; kullanıcıya özel agent'ları ifşa etmez.
- POST /message:send: Message içinden messageId, ROLE_USER ve yalnız plain text parts kabul et; media/file/data parts, context injection ve URL reddedilir. Standard optional tenant alanı seçilmiş AgentInterface Card'da tenant ilan ediyorsa aynı opaque değere eşleşmelidir; yoksa tenant parametresi kabul edilmez. Bu protokol alanı authenticated principal'ın tenant/authorization kararının yerine geçmez. İstek doğrulanmış M2M principal ile allowlist'e bağlanır.
- SendMessage configuration.returnImmediately=true ise task oluşturulduktan sonra hemen dönülebilir. Alan false veya yoksa server terminal ya da interrupted duruma dek beklemelidir; deadline dolarsa working task döndürüp kuralı ihlal etme: kalıcı task'ı koru, 503 retryable google.rpc.Status + RetryInfo / sabit ErrorInfo döndür, aynı messageId ile güvenli retry'ı destekle. Server-side deadline ve client disconnect görevi kendiliğinden silmez.
- GET /tasks/{id}: task creator principal + tenant sahipliği doğrulanır; varsa seçili AgentInterface tenant değeri spec'e uygun query param olarak doğrulanır; yalnız task state ve izin verilen text artifact döndürülür. Stream, cancel, push notification, extended Card, list tasks, multi-turn context, file transfer ve OAuth dance ilk dilimde desteklenmez/advertise edilmez.
A2A terminal state'leri completed, failed, canceled, rejected'tir. input-required ve auth-required interrupted durumlarıdır; internal queued/running dışarıya working map edilir. Task state transition tek yönlü; worker retry aynı task'ı tamamlar.
### Message kimliği ve idempotency
Client messageId stable retry anahtarıdır; unique constraint en az (tenant_id, client_id, message_id) ile tutulur. Aynı key + aynı canonical request digest tekrar gelirse aynı task/sonuç/Location döner; aynı key + farklı payload 409. Duplicate check ve task/queue kaydı tek DB transaction'ında unique constraint ile atomik yapılır. Digest accepted fields üzerinden hesaplanır, bearer dahil edilmez. Retention penceresi konfigüre ve belgeli olsun; idempotency kaydı task'tan kısa yaşayıp duplicate açığı bırakmamalı.
Timeout, connection reset veya worker çökmesiyle sonuç belirsizse aynı messageId ile retry; duplicate güvence verilemiyorsa otomatik yeniden yürütmeyi durdur ve submission_unknown status + audit ile operatör müdahalesi iste. İçerik/sonuç cache'de hassas kabul edilir ve tenant/client ACL'den geçer.
Message content bytes, parts count, task lifetime, queue depth, wait time, result bytes ve response serialization sınırları konfigüre edilir. Desteklenmeyen alanlar sessizce kabul edilmez.

## 8. TLS, CORS, rate limit, worker ve audit
TLS tüm browser/M2M trafiğinde zorunlu; plain HTTP yalnız loopback local development. Reverse proxy forwarded headers yalnız güvenilir proxy IP'lerinden kabul edilir. HSTS HTTPS deployment'ta. DB backup ve session/token secrets erişim kontrolü/encryption-at-rest politikasına bağlı.
CORS varsayılan kapalı. UI aynı origin'den sunulsun; gerekirse exact scheme+host+port origin allowlist, allow_credentials=true, yalnız gerekli method/header. wildcard origin ile cookie/bearer credential asla birlikte kullanılmaz. Preflight auth bypass değildir; Vary: Origin/cache policy doğru ayarlanır. CSRF CORS'tan bağımsızdır.
Rate limit tenant+principal+credential ve IP katmanlarında uygulanır: request/sec, message/day, active tasks, concurrent model calls, bytes ve daily cost. IP tek başına NAT kullanıcılarını cezalandırmamalı. 429 + Retry-After. Process-local limit tek instance içindir; scale öncesi shared store gerekir. Worker her işte server-side tenant/principal context ve queue attempt limitini doğrular.
Audit append-only/erişim kontrollü structured event: request_id, UTC timestamp, action, principal kind/id, tenant, resource type/id, outcome, reason code, credential ID, source IP retention policy, A2A message/task id, byte count, duration, rate decision. Prompt/message/instructions/tool args/results, cookies, bearer, OIDC code/token, DB URL/password ve raw exception body kaydedilmez. App, proxy, provider ve worker logs aynı redaction politikasına tabi; event payload allowlist'tir. Audit retention/integrity işletim dokümanında tanımlanır.
## 9. Uygulama dilimleri ve kabul ölçütleri
Her dilim ayrı issue/PR, feature flag ile kapalı ve migration + rollback adımıyla çıkarılmalı. Authorization matrix testi her protected route için üretilmeli.
1. **Auth/principal:** OIDC server-side login/callback/logout, session store, CSRF, scopes, test issuer. Kabul: PKCE S256/state/nonce ve issuer/audience/expiry negative tests; Secure/HttpOnly/SameSite cookie; session rotation/revoke; CSRF/Origin reddi; anonim iş endpointleri 401.
2. **Tenant migration:** schema migration, legacy tenant backfill, tenant/owner policy. Kabul: dry-run row counts/checksum, restore-tested backup, idempotent migration, no nullable owned rows, FK/index; cross-tenant list/get/update/create/nested testlerinde sızıntı yok.
3. **Resource authorization:** route matrix ve admin policy, tenant agent, parent joins. Kabul: tüm method/path scope matrix allow/deny testlerinde doğru 401/403/404; foreign owner IDs body'de reddedilir; user global config değiştiremez; worker owner binding'i korur.
4. **Machine credentials:** client/credential/scope/tenant registry, hash/revoke/rotate. Kabul: malformed/expired/revoked/wrong-audience/no-scope reddedilir; scope elevation/cross-tenant IDs reddedilir; raw token DB/log/API'de yok; rotation/revoke SLA testi.
5. **Incoming A2A:** spec 1.0 conformance, Card auth announcement, send/task, durable idempotency. Kabul: A2A contract fixture; lifecycle mapping; duplicate aynı payload tek queue job; changed payload aynı key 409; yalnız creator poll eder; unadvertised method reddedilir; feature flag default off.
6. **Abuse/operations:** TLS/proxy/CORS/CSRF/rate budgets/audit/redaction/metrics. Kabul: proxy spoofing denied; exact origin matrix; quota tests 429 + Retry-After; sentinel secrets/prompts application/access/worker logs ve run events'te yok; auth failure/queue depth/unknown submission alerts.
7. **Staged enablement:** private dev → Tailscale canary → restricted clients → wider access review. Her aşama rollback/kill switch. Kabul: penetration checklist, DB restore/revoke drill, canary tenant isolation tests; public DNS/firewall açılımı explicit operator release decision.
Release gate: bağımsız threat-model/security review, dependency scan, authz integration/A2A conformance tests, SQLite + PostgreSQL migration test, restore/revoke drill, log redaction test. Rollback authz'yi kapatıp endpointleri anonim bırakmak olamaz; bakım modu ve eski binary + uyumlu schema planı önceden denenmeli.

## 10. Doğrulama planı
Fixture'larda en az iki tenant, iki human subject, admin, iki M2M client, user/room/run/task graph ve private/global ajan bulunmalı. Property/fuzz testleri path/body id değiştirme, tenant mismatch ve nested joins'i denesin. Her A2A method için official 1.0 sample; malformed JSON/content type/version, unsupported parts, oversized body, duplicate ID, slow client, disconnect, competing duplicates ve replay tests.
Security tests: OIDC wrong issuer/audience/signature/nonce/state/code replay; cookie flags/session fixation; CSRF attacker/null Origin/Fetch Metadata; CORS credential wildcard; stale/revoked session; 403/404 behavior; agent/tool escalation; A2A scope/audience; token in URL/log; DNS rebinding/IP literal/redirect/proxy spoofing; prompt reflect/XSS; queue exhaustion/concurrency.
Migration verification: fixture DB snapshot, production-like copy; dry-run counts, legacy owner mapping, constraints, rollback/restore, old/new API-worker compatibility. Search canary values in API, worker, reverse proxy and provider logs. Disabled feature flag must ensure Card/A2A routes unavailable without side effects. Staging only after backup restore and revoke canary; public listener/firewall remains closed until explicit operational decision.
Operational decisions before code: authoritative OIDC issuer and tenant membership source; owner vs tenant-shared agent policy; machine credential issuance/revocation operator; idempotency retention; quotas/audit retention; deployment origin and trusted proxy CIDRs; DB size/offline migration window. None makes current code multi-user safe automatically.

## 11. Kaynaklar
Standartları implementasyon öncesinde sürüm pinli test fixture'larıyla doğrula:
- [A2A 1.0 Specification](https://a2a-protocol.org/latest/specification/) — Agent Card, HTTP+JSON binding, SendMessage, GetTask, task state. Latest URL değişebilir; implementation sürümü açıkça 1.0 pinlenmeli.
- [RFC 9700: OAuth 2.0 Security Best Current Practice](https://www.rfc-editor.org/rfc/rfc9700.html) — authorization code, PKCE, redirect security.
- [RFC 7636: PKCE](https://www.rfc-editor.org/rfc/rfc7636.html) — code verifier/challenge.
- [RFC 9110: HTTP Semantics](https://www.rfc-editor.org/rfc/rfc9110.html) — 401/403/404/409 semantics; forbidden existence may be hidden with 404.
- [OWASP CSRF Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html) — token, Origin and SameSite controls.
