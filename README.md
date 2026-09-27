# Agent Runtime Platform

> **Durum:** Ajanlar arası mesajlaşma, yeteneğe göre keşif, yerel insan-ajan sohbeti, sınırlandırılmış grup odası/özet akışı, tek alt görev devri ve kalıcı tek sunucu işçisi kullanılabilir. Varsayılan model sağlayıcısı ChatGPT oturumuyla çalışan Codex'tir.

Agent Runtime Platform, yapay zekâ ajanlarını çalışma anında tanımlayıp yönetmek, yeteneklerine göre bulmak ve birbirleriyle izlenebilir biçimde konuşturmak için tasarlanan bir platformdur. Yeni bir ajan eklemek veya devre dışı bırakmak, her seferinde uygulama kodunu değiştirmeyi gerektirmemelidir.

## Ürün hedefi

Bir kullanıcı arayüzünden veya API'den ajan oluştur; modele, talimatlara, yeteneklere, araçlara ve iletişim izinlerine karar ver. Kullanıcı bir konuşma ya da görev başlattığında çalışma zamanı uygun ajanları seçsin, mesajları iletsin, sınırları uygulasın ve olup biteni kaydetsin.

**Temel ilke:** Ajan tanımı veridir; çalışma zamanı bu tanımı okuyarak ajanı çalıştırır. Ajan kayıtları, görevler, konuşmalar, mesajlar ve çalıştırma kayıtları ayrı kavramlardır.

## Bugün kullanılabilenler

- Ajanları API üzerinden oluşturma, düzenleme ve devre dışı bırakma.
- Ajanlar arasında doğrudan mesajlaşma; alıcıyı kimlikle veya tekil yetenek eşleşmesiyle belirleme.
- Yerel, tek kullanıcılı tarayıcı arayüzünde etkin bir ajan seçip insan-ajan sohbeti yapma; geçmiş yenileme sonrasında yüklenir.
- İnsan-ajan sohbetinde bir alt görevi, tam yetenek eşleşmesiyle bulunan tek etkin ajana devretme; bir istekte en fazla bir devir yapılır.
- Çalıştırma durumlarını, kullanılan ajan yapılandırması anlık görüntülerini ve sıralı olay izlerini API üzerinden görüntüleme.

Sınırlı grup odası API’si kullanılabilir. Salt okunur MCP araçları, yalnızca Codex sağlayıcısında ve sunucu yöneticisinin tanımladığı yerel stdio sunucularından etkinleştirilebilir. Sunucu yöneticisi ayrıca belirli yetenekleri uzak A2A 1.0 HTTP+JSON ajanlarına eşleyebilir; yalnızca mevcut tek alt görev devri bu güvenilir katalog üzerinden uzak hedefe gider. MCP sunucularını ve izinli araç adlarını HTTP API’sine göndermek mümkün değildir.

## Kavramsal mimari

```mermaid
flowchart TD
    UI["Web arayüzü / API"] --> CP["Kontrol katmanı"]
    CP --> REG["Ajan ve yetenek kayıtları"]
    CP --> RT["Çalışma zamanı"]
    RT --> ROUTER["Yönlendirme ve kurallar"]
    RT --> PROVIDER["Model sağlayıcı arayüzü"]
    RT --> TOOLS["Araç bağlayıcıları"]
    ROUTER --> STORE["Mesaj, görev ve olay kayıtları"]
    PROVIDER --> LLM["LLM sağlayıcıları"]
    TOOLS --> MCP["MCP / yerel araçlar"]
```

- **Kontrol katmanı:** Ajan tanımlarını, yetenekleri, araç izinlerini ve politikaları saklar; yönetim API'sini sunar.
- **Çalışma zamanı:** Tanımı belirli bir sürüm olarak yükler, modeli ve araçları çağırır, sonucu kaydeder.
- **Yönlendirme:** Mesajı hedef ajana veya yetenek aramasıyla bulunan ajana teslim eder; döngü ve bütçe sınırlarını denetler.
- **Kayıt katmanı:** Konuşmaları, görevleri, mesajları ve çalıştırma olaylarını kalıcı tutar.
- **Bağlayıcılar:** Modelleri ve araçları çalışma zamanından ayırır. MCP yerel araç erişimi sağlar; güvenilir uzak hedeflere giden A2A 1.0 görev devri kullanılabilir. Gelen A2A ve çok kullanıcılı kimlik bu güvenlik tasarımından sonraki uygulama işidir.

Yönlendirme, izin ve limit kontrolleri öngörülebilir kurallarla çalışır. Ajanlar kendilerine verilen görev kapsamında içerik üretir ve izin verilen iletişim kararlarını alır.

## Ajan tanımı

Örnek bir kayıt:

```json
{
  "id": "agent_backend_01",
  "name": "Backend Developer",
  "description": "API development specialist",
  "instructions": "Design and implement backend tasks within the assigned scope.",
  "model": {
    "provider": "configured-provider",
    "name": "configured-model"
  },
  "capabilities": ["backend", "api"],
  "toolIds": ["repository", "filesystem"],
  "communication": {
    "canReceive": true,
    "canSend": true
  },
  "limits": {
    "maxTurns": 10,
    "maxDelegations": 5
  },
  "enabled": true,
  "version": 1
}
```

Bu JSON hedeflenen ajan sözleşmesini gösterir. API'deki tool_ids, /mcp/tools yanıtında listelenen sunucu/araç kimliklerinden seçilir. MCP sunucu komutları, argümanları, ortam değişkeni adları ve yönetici tarafından salt okunur olduğu onaylanan araçlar yalnızca AGENT_RUNTIME_MCP_SERVERS sunucu ortam değişkeninden yüklenir. İstek gövdesiyle komut, URL, yol veya ortam değeri kaydedilemez. OpenAI sağlayıcısı tool_ids kabul etmez; istek açık bir 422 hatası alır.

MCP katalog yapılandırma örneği:

    {"docs":{"command":"uvx","args":["example-readonly-mcp"],"env_vars":["DOCS_TOKEN"],"read_only_tools":["search","fetch"]}}

Bu JSON'u AGENT_RUNTIME_MCP_SERVERS ortam değişkenine koy. Token değerleri ayrı süreç ortam değişkenlerinde tutulur ve API yanıtlarına ya da çalıştırma izlerine eklenmez. read_only_tools güven kararıdır: MCP readOnlyHint açıklama niteliğindedir ve tek başına yetki vermez. İlk sürüm Codex'e yalnızca ajanın açık izin listesindeki araçları verir; shell, birleşik çalıştırma, web araması ve diğer MCP sunucuları kapalı kalır. İzin kaldırma, kuyruğa alınmış iş Codex'i başlatmadan önce güncel ajan kaydıyla tekrar denetlenir. Araç çağrısı izi yalnızca sunucu, araç, durum ve aşama alanlarını tutar. Araç argümanları, sonuç içeriği ve gizli değerler kaydedilmez. Codex oturumu, kullanıcı genelindeki ~/.codex/config.toml dosyasını devralmaz: kimlik doğrulama dosyası yalnızca uygulamanın ~/.agent-runtime-platform/codex-home dizinine kopyalanır (dizin 0700, dosya 0600); uygulama tarafından yenilenen belirteçler bu kopyada kalır.

Bir ajan devre dışı bırakıldığında eski konuşmaların ajan kimliği korunur. Her çalıştırma, kullanılan ajan tanımının sürümünü veya anlık görüntüsünü kaydeder; böylece geçmiş sonuçlar daha sonra açıklanabilir.

## Ajanlar nasıl konuşacak?

### Doğrudan mesaj

Bir ajan izin verilen başka bir ajana görev veya soru gönderir. Mesaj, konuşma ve varsa görev kimliğiyle saklanır.

### Yeteneğe göre keşif

Gönderen, sabit bir ajan adı yerine `postgresql` gibi bir yetenek isteyebilir. Kayıt katmanı uygun ve etkin adayları bulur; yönlendirme kuralları hedefi seçer. Birden çok aday veya hiç aday bulunmaması açıkça ele alınır.

### Devir ve koordinasyon

İnsan-ajan sohbetinde üst ajan bir sınırlı alt görevi tam yetenek eşleşmesiyle bulunan tek etkin ajana devredebilir. Üst ajan kullanıcıya yanıt verir; üst/alt görev ilişkisi, kullanılan ajan yapılandırması anlık görüntüsü, durum, sonuç ve çalıştırma olayları kaydedilir. Her kullanıcı isteğinde en fazla bir devir yapılır; devredilen ajan yeni bir devir başlatamaz. Birden çok ajana açık uçlu koordinasyon yerine sınırlı oda akışı kullanılır.

### Oda / grup konuşması

Grup odasında 2–5 kayıtlı ajan aynı görev için belirlenen sırada birer katkı üretir; seçilen moderatör bu katkılardan tek bir son yanıt hazırlar. Her çalıştırma en fazla altı model çağrısı yapar; araç kullanımı ve alt göreve devir kapalıdır.

Örnek mesaj zarfı:

```json
{
  "id": "msg_123",
  "conversationId": "conv_987",
  "taskId": "task_123",
  "fromAgentId": "agent_analyst",
  "toAgentId": "agent_backend_01",
  "type": "task",
  "content": { "objective": "Design an authentication API" }
}
```

**Konuşma**, katılımcıların ortak bağlamıdır. **Görev**, yapılacak işin hedefi ve durumudur. Bir konuşmada birden fazla görev bulunabilir; bir görevin alt görevleri olabilir.

## Önerilen veri modeli

| Kayıt | Sorumluluk |
| --- | --- |
| `agents` | Kimlik, açıklama, talimat, model seçimi, durum ve sürüm |
| `agent_capabilities` | Yetenek tanımları ve eşleştirmeleri |
| `agent_tools` | Ajanın kullanmasına izin verilen araçlar |
| `agent_relations` | Gerekirse açık iletişim izinleri ve ilişkileri |
| `conversations` | Konuşma kimliği ve yaşam döngüsü |
| `conversation_members` | Konuşmaya katılan ajanlar |
| `messages` | Gönderen, alıcı, içerik ve zaman damgası |
| `tasks` | Hedef, atanan ajan, üst görev ve durum |
| `runs` | Bir ajan çalıştırmasının durumu ve ajan sürümü |
| `run_events` | Çalıştırma zaman çizelgesi ve hata ayıklama olayları |

Gerçek şema, gereksinimler ve ilk uygulama sırasında belirlenecek. Bir ajanın kullanıcı arayüzündeki “sil” işlemi geçmiş referansları kırmamak için devre dışı bırakma veya yumuşak silme olarak uygulanmalıdır.

## Sınırlar ve güvenilirlik

- Her mesajda gönderen, hedef, konuşma, görev ve izleme kimlikleri taşınır.
- Ajanların iletişim ve araç kullanımı izinlerle sınırlandırılır; model çıktısı tek başına yetki vermez.
- İnsan-ajan sohbetinde devir sayısı en fazla bir olacak şekilde kuralla sınırlandırılır. Tur, süre ve maliyet limitleri ile genel döngü tespiti henüz uygulanmamıştır.
- Teslimat ve tekrar denemeleri aynı görevin yanlışlıkla iki kez sonuç üretmesini önleyecek biçimde tasarlanır.
- Hatalar, denemeler ve kararlar olay kayıtlarında izlenir; gizli bilgiler loglara yazılmaz.
- Çalışma sırasındaki bir ajan değişikliği, başlamış çalıştırmanın sürümünü geriye dönük değiştirmez.

## İlk çalışan dilim

İlk uygulama; ajanları API üzerinden kaydeder, bir konuşmaya iki ajan ekler ve bir ajanın diğerine gönderdiği mesajı tek bir genel LangGraph akışı üzerinden çalıştırır. Model seçimi ve talimatlar kayıtlı ajan tanımından yüklenir. Bir ajana özel Python sınıfı veya ayrı derlenmiş grafik gerekmez.

Mesajlar, çalıştırmalar, ajan tanımı anlık görüntüleri ve sıralı olaylar veritabanında saklanır. Varsayılan sağlayıcı, sunucudaki ChatGPT girişiyle çalışan resmî Codex Python SDK'dır. OpenAI API anahtarıyla çalışan eski sağlayıcı isteğe bağlı olarak kullanılabilir. Otomatik testler sahte sağlayıcı kullanır ve gerçek bir model çağrısı yapmaz.

### Gereksinimler

- Python 3.11 veya üstü
- [uv](https://docs.astral.sh/uv/)
- Gerçek model yanıtları için sunucuda ChatGPT hesabıyla giriş yapılmış Codex oturumu

### Yerelde çalıştırma

```bash
uv sync --extra dev
cp .env.example .env
uv run --locked agent-runtime-login
```

Giriş açıksa komut durumu gösterir; değilse Codex SDK üzerinden cihaz kodu ve doğrulama adresi verir. Global Codex CLI kurmanız gerekmez. `.env` yalnızca yerel veritabanı ayarını içerir; Codex oturum bilgileri buraya kopyalanmaz. Ajan kayıtları varsayılan olarak `data/agent_runtime.db` SQLite veritabanında tutulur. Ardından:

```bash
uv run uvicorn agent_runtime_platform.main:app --app-dir src --reload
```

Async mesajları tüketmek için ikinci bir terminalde tek worker başlatın:

```bash
uv run agent-runtime-worker
```

API belgeleri `http://127.0.0.1:8000/docs` adresinde açılır. Kontrolleri çalıştırmak için:

```bash
uv run pytest
```

Tarayıcı sohbetini `http://127.0.0.1:8000/` adresinden açın. İlk ajanı oluşturmak için soldaki formdan Codex modelini, o modelin desteklediği düşünme eforunu ve talimatları seçin; ardından ajanla mesajlaşabilirsiniz. Model ve efor seçenekleri sunucudaki Codex SDK kataloğundan alınır. Sohbet geçmişi veritabanında tutulur ve sayfa yenilendiğinde yüklenir.

PostgreSQL kullanmak için `AGENT_RUNTIME_DATABASE_URL` değerini örneğin `postgresql+psycopg://user:password@localhost:5432/agent_runtime` olarak ayarlayın.

### Oracle sunucuda kalıcı servis ve telefondan erişim

Bu depo `/home/opc/apps/agent-runtime-platform` konumunda kuruluysa, `uv sync --extra dev --locked` ve `uv run --locked agent-runtime-login` adımlarından sonra `opc` kullanıcısı altında systemd servisini kurun:

```bash
mkdir -p ~/.config/systemd/user
install -m 644 deploy/systemd/user/agent-runtime-platform.service ~/.config/systemd/user/agent-runtime-platform.service
systemctl --user daemon-reload
systemctl --user enable --now agent-runtime-platform.service
# Kalıcı asenkron mesajları tüketmek için işçiyi de kurun.
install -m 644 deploy/systemd/user/agent-runtime-worker.service ~/.config/systemd/user/agent-runtime-worker.service
systemctl --user daemon-reload
systemctl --user enable --now agent-runtime-worker.service
curl -fsS http://127.0.0.1:8000/health
```

`loginctl show-user opc -p Linger` çıktısı `Linger=yes` olmalıdır; `no` ise `sudo loginctl enable-linger opc` komutu kullanıcı servisinin SSH oturumu kapandıktan sonra da çalışmasını sağlar. Servis yalnızca `127.0.0.1:8000` adresini dinler. Telefonda özel erişim için [Tailscale'in Oracle Linux 9 paketini](https://dl.tailscale.com/stable/) kurup aynı özel ağa bağlanın:

```bash
sudo dnf config-manager --add-repo https://pkgs.tailscale.com/stable/oracle/9/tailscale.repo
sudo dnf install -y tailscale
sudo systemctl enable --now tailscaled
sudo tailscale up
sudo tailscale serve --bg 8000
sudo tailscale serve status
```

`tailscale up` tarafından verilen bağlantıdan sunucuya giriş yapın. [iPhone'a Tailscale'i kurup](https://tailscale.com/docs/install/ios) aynı hesaba giriş yaptığınızda `tailscale serve status` çıktısındaki özel HTTPS adresini açın. Uygulamanın kendi kullanıcı girişi henüz yoktur; bu adres yalnızca özel ağ üyelerine açılır.

### API akışı

1. `POST /agents` ile iki ajan oluşturun. Her biri için `name`, `instructions`, `model_provider` (varsayılan `codex`) ve `model_name` gerekir. Codex için `GET /codex/models` model ve desteklenen eforları listeler; `model_reasoning_effort` (örneğin `high`) isteğe bağlıdır ve verilmezse modelin varsayılanı kullanılır. `capabilities` isteğe bağlıdır; örneğin `{"capabilities": ["backend", "api"]}`.
2. `POST /conversations` ile `agent_ids` listesini gönderin.
3. `GET /agents?capability=backend` ile bu yeteneğe sahip etkin ajanları arayın. Eşleşme tamdır; yetenekler kaydedilirken ve aranırken boşluklardan arındırılıp küçük harfe dönüştürülür.
4. `POST /conversations/{conversation_id}/messages` isteğinde `sender_agent_id`, `content` ve alıcılardan yalnızca birini gönderin: `recipient_agent_id` veya `recipient_capability`. Bu mevcut senkron uçtur. Kalıcı async kabul için aynı gövdeyi `/conversations/{conversation_id}/messages/async` adresine gönderin; `202` yanıtında `id`, `status` ve `status_url` bulunur. Yetenek eşleşmesi tek bir etkin ajan bulursa o ajan konuşmaya otomatik eklenir. Hiç eşleşme yoksa `404`, birden fazla eşleşme varsa `409` döner. Kimlikle gönderimde iki ajan da önceden konuşma üyesi olmalıdır.
5. `GET /runs/{run_id}` üzerinden mesajları, görev kayıtlarını, durum bilgisini ve olay izini okuyun. Async durum `queued`, `running`, `completed` veya `failed` olur. Konuşmanın tamamı `GET /conversations/{conversation_id}` üzerinden alınabilir.

`PATCH /agents/{agent_id}` ajan ayarlarını, yeteneklerini değiştirir veya `{"enabled": false}` ile yeni çalıştırmalarda kullanılmasını engeller. Her değişiklik ajan sürümünü artırır. Çalışan her görev, başlangıçta kullandığı talimat/model/yetenek anlık görüntüsünü saklar; çalıştırma API'si talimat içeriğini döndürmez.

### Grup odası API'si

- `POST /rooms` gövdesi `{"name":"Tasarım incelemesi","participant_agent_ids":["ajan-1","ajan-2"],"moderator_agent_id":"ajan-1"}` ile oda oluşturur. Katılımcı sırası listedeki sıradır; 2–5 etkin ve benzersiz ajan gerekir, moderatör katılımcılardan biri olmalıdır.
- `GET /rooms/{room_id}` oda ayarını ve başlangıçtaki ajan yapılandırması anlık görüntülerini döndürür.
- `POST /rooms/{room_id}/runs` gövdesi `{"content":"Görev açıklaması"}` ile kalıcı kuyruğa ekler ve hemen `202` ile `{"id":"...","status":"queued","status_url":"/runs/..."}` döndürür.
- `GET /runs/{run_id}` eski bire bir çalıştırmaların mevcut yanıtını korur; oda çalıştırmalarında ek olarak `run_type: "room"`, sıralı `turns` (katılımcı ve moderatör özeti `phase` alanıyla ayrılır), `final_answer` ve olay izini döndürür. `GET /rooms/{room_id}/runs` oda çalıştırma geçmişini listeler.
- İşçi her katılımcıyı birer kez sırayla çağırır, sonra moderatörden tek özet ister. Her tur önceki katkıları görür; devir ve araç kullanımı kapalıdır. Normal çalıştırmada üst sınır altı model çağrısıdır. Tamamlanan katkılar retry/yeniden başlatmada atlanır; devam eden model çağrısının bir kez çalışması garanti edilmez.

### İnsan-ajan sohbet API'si

- `POST /chat/conversations` gövdesi `{"agent_id": "..."}` ile tek ajanlı bir sohbet başlatır.
- `POST /chat/conversations/{conversation_id}/messages` gövdesi `{"content": "..."}` ile senkron kullanıcı mesajı gönderir; mevcut davranış korunur.
- `POST /chat/conversations/{conversation_id}/messages/async` aynı gövdeyi kalıcı kuyruğa ekler ve `202` ile run kimliği döndürür.
- `GET /conversations/{conversation_id}` sohbet geçmişini, `GET /runs/{run_id}` görev durumları ve sonuçları dâhil son yanıtın çalıştırma izini döndürür.
- Ajan bir sınırlı alt görevi tam yetenek eşleşmesi olan tek etkin ajana devredebilir; her çalıştırmada en fazla bir devir yapılır.

İşçi, `agent-runtime-worker.service` systemd kullanıcı servisi olarak API'den ayrı çalışır. SQLite `queue_jobs` tablosu ilk veritabanı açılışında eklenir; mevcut veriler için yeniden oluşturma veya silme yapılmaz. Veritabanı başına işletim sistemi kilidi ikinci bir yerel worker'ın işleri sahiplenmesini veya toparlamasını engeller. Systemd durdurma isteğinde `KillMode=mixed` önce yalnızca worker ana sürecine sinyal gönderir; worker yeni iş almayı bırakıp o anki model çağrısının bitmesini en fazla 300 saniye bekler, zaman aşımında süreç grubu sonlandırılır. Ani kapanma/sert sonlandırma yarım kalan bir denemeyi tüketir; başlangıçta toparlanan işler toplamda en fazla üç denemeyle sıraya alınır. Model çağrısı tekrar çalıştırılabilir ve model/gelecekteki araç yan etkileri tam olarak bir kez garantili değildir. Çalıştırma başına kullanıcıya görünen yanıt ve devredilmiş alt görev kaydı yinelenmeye karşı korunur; tekrar denemede ilk kaydedilmiş devir hedefi ve amacı kullanılır. Başarısız işler `failed` durumuna geçer.

Web arayüzü yerel ve tek kullanıcılı kullanım içindir. Uygulama kimlik doğrulaması varsayılan olarak kapalıdır; tek legacy kullanıcı için OIDC kapısı kurulum başına açıkça etkinleştirilebilir. Bu kapı veri sahipliği veya tenant izolasyonu sağlamaz; tenant migration ve kaynak yetkilendirme tamamlanmadan çok kullanıcılı ya da genel internet erişimine açmayın.

#### İsteğe bağlı OIDC tek kullanıcı kapısı

Varsayılan `AGENT_RUNTIME_AUTH_MODE=off` mevcut yerel/tek kullanıcılı davranışı korur. OIDC yalnız uygulama tek API süreci çalıştırırken, operatörün sabit issuer'ı, client ID'si, HTTPS callback adresi ve mevcut tek kullanıcının değişmez `sub` değeri sağlandığında etkinleştirilir. `.env` dosyasında (veya deployment secret/config kaynağında) aşağıdaki değerleri ayarlayın; gerçek değerleri git'e eklemeyin:

```dotenv
AGENT_RUNTIME_AUTH_MODE=oidc
AGENT_RUNTIME_OIDC_ISSUER=https://id.example.com/issuer
AGENT_RUNTIME_OIDC_CLIENT_ID=agent-runtime
AGENT_RUNTIME_OIDC_REDIRECT_URI=https://runtime.example.com/auth/callback
AGENT_RUNTIME_OIDC_LEGACY_SUB=the-exact-existing-operator-subject
AGENT_RUNTIME_OIDC_LEGACY_TENANT=legacy
AGENT_RUNTIME_OIDC_SESSION_TTL_SECONDS=28800
```

Provider, Authorization Code + PKCE S256, `openid` scope ve ID token'ı desteklemelidir. Bu uygulama public OIDC client olarak çalışır: client provider'da `token_endpoint_auth_method=none` ve PKCE ile kaydedilmeli; confidential-client secret kabul edilmez. `azp` claim'i varsa her zaman `client_id` ile eşleşmeli, çoklu audience için de zorunludur. Discovery/token/JWKS uçları aynı sabit issuer origin'inde olmalı; otomatik HTTP yönlendirmeleri izlenmez. Üretimde callback HTTPS olmalıdır. OIDC kapısı yalnız yapılandırılmış `iss/sub` kimliğine session açar; ikinci bir kişi, Authorization bearer/M2M çağrısı ve anonim iş API'si reddedilir. OIDC açıkken `/docs`, `/redoc` ve `/openapi.json` kapalıdır. Giriş sonrası cookie host-only, Secure, HttpOnly ve SameSite=Lax'tır; uygulama oturumu sunucu tarafında tutar ve durum değiştiren isteklerde session'a bağlı CSRF başlığı ile tam Origin eşleşmesi ister. `GET /auth/session` arayüzün CSRF başlığını bellekte hazırlamasını sağlar; `POST /auth/logout` oturumu iptal eder. Başarılı yeniden giriş önceki browser session'ını iptal eder; başarısız giriş mevcut session'ı korur. Session sona ererse iş API'sinden gelen `401` arayüzü yeniden giriş ekranına götürür. Yapılandırma issuer/subject/tenant olarak değişirse eski session'lar artık kabul edilmez. Yalnızca client ID değiştirilmesi mevcut session'ları iptal etmez; kullanıcı logout olana veya mutlak session ömrü dolana kadar geçerli kalırlar.

Bu ilk dilim mevcut kayıtları bir kullanıcıya migrate etmez ve endpoint'lere owner/tenant filtresi eklemez. Kapatmak için `AGENT_RUNTIME_AUTH_MODE=off` yapıp API sürecini yeniden başlatın; oturum tablosu yalnızca yeni `auth_sessions` tablosudur ve mevcut tablolara kolon eklenmez. Dağıtımdan önce SQLite için tutarlı yedek alın. Tüm kimlik yapılandırması ve OIDC gerçek sağlayıcıyla doğrulanmadan genel ağ erişimi açmayın.

İstek gövdesi ve hata biçimleri için `/docs` içindeki OpenAPI arayüzünü kullanın. `codex` sağlayıcısı resmî Codex SDK üzerinden sunucudaki mevcut ChatGPT oturumunu kullanır; API anahtarı gerekmez. Ajan çağrıları salt okunur sandbox içinde, komut ve web araçları kapalı olarak yürütülür. Codex kullanım limitleri ChatGPT planına bağlıdır. Eski `openai` sağlayıcısını özellikle seçerseniz ayrıca `OPENAI_API_KEY` ayarlamanız ve API kullanımını karşılamanız gerekir. Testler hiçbir canlı model servisine bağlanmaz.

## MVP kapsamı ve uygulama durumu

| İşlev | Mevcut durum |
| --- | --- |
| Ajan yönetimi | API üzerinden ajan oluşturma, düzenleme ve devre dışı bırakma kullanılabilir. |
| Model seçimi | Varsayılan Codex (ChatGPT girişi) ve isteğe bağlı OpenAI API sağlayıcısı ile ajan başına model adı desteklenir. OpenAI sağlayıcısı MCP araç izni almayı reddeder. |
| Yeteneğe göre keşif | Etkin ajanlar tam yetenek eşleşmesiyle aranır; tekil olmayan veya boş eşleşme açık hata verir. |
| Ajanlar arası mesajlaşma | İki ajan arasında kimlikle veya tekil yetenek eşleşmesiyle doğrudan mesajlaşma kullanılabilir. |
| İnsan-ajan sohbeti | Yerel tek kullanıcılı arayüzden sohbet başlatılır; konuşma geçmişi kalıcıdır. |
| Çalıştırma izi | Durum, ajan yapılandırması anlık görüntüsü, MCP izin kontrolü ve sırayla eklenen araç çağrısı olayları API'den okunabilir. |
| Kalıcı arka plan kuyruğu | Yeni async API uçları ve ayrı tek sunucu işçisi kullanılabilir; sınırlı yeniden deneme ve başlangıç toparlaması uygulanır. |
| Grup odası | 2–5 kayıtlı ajan açık sırayla bir tur katkı verir; seçilen moderatör katkılardan tek bir son yanıt üretir. Kuyruk ve çalışma izi kalıcıdır. |
| Görev devri | İnsan-ajan sohbetinde en fazla bir alt görev tek yerel veya güvenilir A2A ajanına devredilir; üst/alt görev ilişkisi, hedef anlık görüntüsü, uzak kimlik/durum, sonuç ve olay izi saklanır. |
| Auth/principal | Varsayılan kapalı, tek sabit OIDC legacy kimliğine sahip server session + CSRF kapısı; tenant migration #34 ayrı offline araçla hazırlanıyor, kaynak authorization yok. |

### Giden A2A devri

Uzak A2A hedefleri yalnızca sunucu yöneticisinin `AGENT_RUNTIME_A2A_TARGETS` ortam değişkeninde tanımlanır; model ve API gövdeleri hedef URL’si sağlayamaz. Güvenli katalog, `GET /a2a/targets` üzerinden yalnızca hedef kimliği, `kind: "a2a"` ve yetenekleri gösterir. İnsan-ajan sohbetinde model, yönetici tarafından tanımlanmış uzak yetenekleri devir isteminde kullanabilir; aynı yetenek birden fazla yerel/uzak hedefle eşleşirse istek reddedilir.

Tam ortam değişkeni şeması, Agent Card doğrulaması, A2A v1 taşıma ayrıntıları ve gönderim/tekrar semantiği için [giden A2A belgesine](docs/a2a.md) bakın. Kartlar `HTTP+JSON` ve protokol `1.0` ilan etmelidir. Kimlik bilgileri yalnızca adlandırılmış sunucu ortam değişkeninden okunur; API yanıtlarına, görev anlık görüntülerine veya olaylara yazılmaz.

Kalıcı kuyruk ilk sürümde SQLite ile aynı sunucuda çalışan tek bir işçi sürecini kullanır. Dağıtık işçiler ve harici kuyruk altyapısı bu kapsamda yoktur.

## Sonraki aşamalar

1. Auth/principal dilimi (#32, PR #33): varsayılan kapalı OIDC PKCE public-client login, browser-bound state, sabit tek legacy `iss/sub`, server session/CSRF kapısı. Bu, çok kullanıcılı erişim değildir ve tenant/resource izolasyonu içermez.
2. Tenant migration (#34): sürümlü offline backfill aracı ve tek legacy owner yazma koruması PR #34 ile hazırlanıyor; canlı owner eşlemesi henüz doğrulanmadı. Kaynak authorization sonraki ayrı güvenlik dilimidir.
3. Kaynak yetkilendirme ve tenant-scope route matrisini tamamla; bu kontroller bitmeden gelen A2A'yı ve genel internet erişimini etkinleştirme.
4. Dağıtık kuyruk kararını yalnızca ölçümler veya açık bir çok-host/yüksek erişilebilirlik gereksinimi mevcut tasarımı yetersiz kıldığında yeniden değerlendir: [karar ve ölçüm kapısı](docs/queue-scaling-decision.md).
5. Zamanlanmış görevler, bellek, onay akışları ve görsel ajan ilişkileri editörü.

## Teknoloji yönü

**Kararlar:** Ajan akışlarını çalıştırmak için LangGraph, HTTP API için FastAPI, veriye erişim için SQLAlchemy kullanılacak. Yerel kurulum SQLite ile başlar; aynı şema PostgreSQL'e de bağlanabilir. Ajan tanımları kayıt katmanında veri olarak tutulur ve ortak LangGraph akışı bunları çalıştırma anında yükler. Varsayılan model sağlayıcısı, ChatGPT oturumunu kullanan Codex'tir; OpenAI API sağlayıcısı da isteğe bağlıdır.

Ürün katmanı ajan kataloğu, izinler, konuşmalar, mesajlaşma ve oda davranışlarından sorumlu olur. Yerel tek kullanıcılı sohbet arayüzü ve tek sunuculu kalıcı görev kuyruğu kullanılabilir; kapsamlı yönetim/operatör arayüzü daha sonra değerlendirilebilir.

Mevcut dilimler API, yerel sohbet arayüzü, tek işçili kalıcı kuyruk ve oda oluşturma/geçmiş/çalıştırma izleme arayüzüyle sınırlı grup odası sunar. MCP salt okunur araç dilimi ve güvenilir hedeflere giden A2A 1.0 dilimi tamamlandı. Auth/principal ilk dilimi varsayılan kapalı tek-kullanıcı OIDC kapısıdır. Issue #34 tenant migration PR ile hazırlanıyor; canlı backfill ve PostgreSQL doğrulaması release gate olarak açık. Resource authorization henüz yoktur. Dağıtık kuyruk ancak ölçülen yük bunu gerektirirse ele alınır.
