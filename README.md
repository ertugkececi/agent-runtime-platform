# Agent Runtime Platform

> **Durum:** Ajanlar arası mesajlaşma, yeteneğe göre keşif, yerel insan-ajan sohbeti ve sınırlandırılmış tek alt görev devri kullanılabilir. Varsayılan model sağlayıcısı ChatGPT oturumuyla çalışan Codex'tir.

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

Grup odaları, araç/MCP bağlayıcıları, arka plan işçileri ve çok kullanıcılı erişim henüz uygulanmadı. Planlanan işler “Sonraki aşamalar” bölümünde yer alır.

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
- **Bağlayıcılar:** Modelleri ve araçları çalışma zamanından ayırır. MCP araç erişimi için, A2A ise ileride uzak ajan sistemleriyle birlikte çalışabilirlik için değerlendirilebilir.

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

Bu JSON, hedeflenen geniş ajan sözleşmesini gösterir. Uygulama API'si bugün ajan adı, açıklaması, talimatları, model sağlayıcısı ve adı, etkin durumu ve yeteneklerini yönetir; çalıştırmalar kullanılan sürümlü yapılandırmanın anlık görüntüsünü saklar. Araç kimlikleri, iletişim izinleri ve limit alanları henüz API'de uygulanmamıştır. Anahtarlar ve erişim belirteçleri ajan tanımına düz metin olarak yazılmamalıdır.

Bir ajan devre dışı bırakıldığında eski konuşmaların ajan kimliği korunur. Her çalıştırma, kullanılan ajan tanımının sürümünü veya anlık görüntüsünü kaydeder; böylece geçmiş sonuçlar daha sonra açıklanabilir.

## Ajanlar nasıl konuşacak?

### Doğrudan mesaj

Bir ajan izin verilen başka bir ajana görev veya soru gönderir. Mesaj, konuşma ve varsa görev kimliğiyle saklanır.

### Yeteneğe göre keşif

Gönderen, sabit bir ajan adı yerine `postgresql` gibi bir yetenek isteyebilir. Kayıt katmanı uygun ve etkin adayları bulur; yönlendirme kuralları hedefi seçer. Birden çok aday veya hiç aday bulunmaması açıkça ele alınır.

### Devir ve koordinasyon

İnsan-ajan sohbetinde üst ajan bir sınırlı alt görevi tam yetenek eşleşmesiyle bulunan tek etkin ajana devredebilir. Üst ajan kullanıcıya yanıt verir; üst/alt görev ilişkisi, kullanılan ajan yapılandırması anlık görüntüsü, durum, sonuç ve çalıştırma olayları kaydedilir. Her kullanıcı isteğinde en fazla bir devir yapılır; devredilen ajan yeni bir devir başlatamaz. Birden çok ajana koordinasyon bu dilimde yoktur.

### Oda / grup konuşması

Bir konuşmaya birden fazla ajan katılabilir. Örneğin mimar, geliştirici ve güvenlik ajanı aynı konuda görüş belirtebilir. Grup odaları henüz uygulanmadı; planlanan çözümde söz hakkı sırası ve bitiş koşulu çalışma zamanı tarafından belirlenecek, kontrolsüz ve sonsuz ajan konuşmaları engellenecek.

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
4. `POST /conversations/{conversation_id}/messages` isteğinde `sender_agent_id`, `content` ve alıcılardan yalnızca birini gönderin: `recipient_agent_id` veya `recipient_capability`. Yetenek eşleşmesi tek bir etkin ajan bulursa o ajan konuşmaya otomatik eklenir. Hiç eşleşme yoksa `404`, birden fazla eşleşme varsa `409` döner. Kimlikle gönderimde iki ajan da önceden konuşma üyesi olmalıdır.
5. Yanıttaki çalıştırma kimliğiyle `GET /runs/{run_id}` üzerinden mesajları, görev kayıtlarını, durum bilgisini ve olay izini okuyun. Konuşmanın tamamı `GET /conversations/{conversation_id}` üzerinden alınabilir.

`PATCH /agents/{agent_id}` ajan ayarlarını, yeteneklerini değiştirir veya `{"enabled": false}` ile yeni çalıştırmalarda kullanılmasını engeller. Her değişiklik ajan sürümünü artırır. Çalışan her görev, başlangıçta kullandığı talimat/model/yetenek anlık görüntüsünü saklar; çalıştırma API'si talimat içeriğini döndürmez.

### İnsan-ajan sohbet API'si

- `POST /chat/conversations` gövdesi `{"agent_id": "..."}` ile tek ajanlı bir sohbet başlatır.
- `POST /chat/conversations/{conversation_id}/messages` gövdesi `{"content": "..."}` ile kullanıcı mesajını gönderir.
- `GET /conversations/{conversation_id}` sohbet geçmişini, `GET /runs/{run_id}` görev durumları ve sonuçları dâhil son yanıtın çalıştırma izini döndürür.
- Ajan bir sınırlı alt görevi tam yetenek eşleşmesi olan tek etkin ajana devredebilir; her çalıştırmada en fazla bir devir yapılır.

Web arayüzü yerel ve tek kullanıcılı kullanım içindir; kimlik doğrulama ve çok kullanıcılı erişim bu dilimde yoktur.

İstek gövdesi ve hata biçimleri için `/docs` içindeki OpenAPI arayüzünü kullanın. `codex` sağlayıcısı resmî Codex SDK üzerinden sunucudaki mevcut ChatGPT oturumunu kullanır; API anahtarı gerekmez. Ajan çağrıları salt okunur sandbox içinde, komut ve web araçları kapalı olarak yürütülür. Codex kullanım limitleri ChatGPT planına bağlıdır. Eski `openai` sağlayıcısını özellikle seçerseniz ayrıca `OPENAI_API_KEY` ayarlamanız ve API kullanımını karşılamanız gerekir. Testler hiçbir canlı model servisine bağlanmaz.

## MVP kapsamı ve uygulama durumu

| İşlev | Mevcut durum |
| --- | --- |
| Ajan yönetimi | API üzerinden ajan oluşturma, düzenleme ve devre dışı bırakma kullanılabilir. |
| Model seçimi | Varsayılan Codex (ChatGPT girişi) ve isteğe bağlı OpenAI API sağlayıcısı ile ajan başına model adı desteklenir; araç yapılandırması uygulanmadı. |
| Yeteneğe göre keşif | Etkin ajanlar tam yetenek eşleşmesiyle aranır; tekil olmayan veya boş eşleşme açık hata verir. |
| Ajanlar arası mesajlaşma | İki ajan arasında kimlikle veya tekil yetenek eşleşmesiyle doğrudan mesajlaşma kullanılabilir. |
| İnsan-ajan sohbeti | Yerel tek kullanıcılı arayüzden sohbet başlatılır; konuşma geçmişi kalıcıdır. |
| Çalıştırma izi | Durum, ajan yapılandırması anlık görüntüsü ve sıralı olaylar API'den okunabilir. |
| Grup odası | Birden fazla ajanın aynı konuşmada koordineli çalışması planlanıyor. |
| Görev devri | İnsan-ajan sohbetinde en fazla bir alt görev tek etkin ajana devredilir; üst/alt görev ilişkisi, ajan anlık görüntüsü, sonuç ve olay izi saklanır. |

Bir sonraki uçtan uca hedef: Görevleri kalıcı kuyruk ve dağıtık çalışanlarla yürüterek servis yeniden başlasa da çalışma durumunu güvenilir biçimde sürdürmek.

## Sonraki aşamalar

1. Kalıcı kuyruk ve dağıtık işçiler; yük arttığında Redis Streams, NATS veya benzeri bir bileşen değerlendirmesi.
2. Grup odaları ve birden fazla ajanın koordinasyonu.
3. MCP araç bağlayıcıları ve uzak ajan sistemleriyle A2A uyumluluğu.
4. Zamanlanmış görevler, bellek, onay akışları ve görsel ajan ilişkileri editörü.

## Teknoloji yönü

**Kararlar:** Ajan akışlarını çalıştırmak için LangGraph, HTTP API için FastAPI, veriye erişim için SQLAlchemy kullanılacak. Yerel kurulum SQLite ile başlar; aynı şema PostgreSQL'e de bağlanabilir. Ajan tanımları kayıt katmanında veri olarak tutulur ve ortak LangGraph akışı bunları çalıştırma anında yükler. Varsayılan model sağlayıcısı, ChatGPT oturumunu kullanan Codex'tir; OpenAI API sağlayıcısı da isteğe bağlıdır.

Ürün katmanı ajan kataloğu, izinler, konuşmalar, mesajlaşma ve oda davranışlarından sorumlu olacak. Yerel tek kullanıcılı sohbet arayüzü mevcuttur; kapsamlı yönetim/operatör arayüzü daha sonra değerlendirilebilir. İlk aşamada ayrı mesaj kuyruğu eklenmeyecek.

Mevcut dilimler API ve yerel sohbet arayüzü sunar. Grup odaları, çok kullanıcılı erişim ve kimlik doğrulama, kuyrukta çalışan işler, araç bağlayıcıları/MCP, A2A ve veritabanı migration yönetimi sonraki işlerin kapsamındadır.
