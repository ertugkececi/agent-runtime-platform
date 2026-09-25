# Agent Runtime Platform

> **Durum:** İlk çalışan API dilimi eklendi. Ajanları kaydedip iki ajan arasında izlenebilir doğrudan mesaj çalıştırabilirsiniz.

Agent Runtime Platform, yapay zekâ ajanlarını çalışma anında tanımlayıp yönetmek, yeteneklerine göre bulmak ve birbirleriyle izlenebilir biçimde konuşturmak için tasarlanan bir platformdur. Yeni bir ajan eklemek veya devre dışı bırakmak, her seferinde uygulama kodunu değiştirmeyi gerektirmemelidir.

## Ürün hedefi

Bir kullanıcı arayüzünden veya API'den ajan oluştur; modele, talimatlara, yeteneklere, araçlara ve iletişim izinlerine karar ver. Kullanıcı bir konuşma ya da görev başlattığında çalışma zamanı uygun ajanları seçsin, mesajları iletsin, sınırları uygulasın ve olup biteni kaydetsin.

**Temel ilke:** Ajan tanımı veridir; çalışma zamanı bu tanımı okuyarak ajanı çalıştırır. Ajan kayıtları, görevler, konuşmalar, mesajlar ve çalıştırma kayıtları ayrı kavramlardır.

## İlk sürümde kullanıcı ne yapabilecek?

1. Arayüz veya API üzerinden ajan oluşturacak, düzenleyecek ve devre dışı bırakacak.
2. Ajan için model sağlayıcısı, model, talimat, yetenek ve izin verilen araçları tanımlayacak.
3. Ajanlar arasında doğrudan mesaj gönderecek ve bir ajanın başka ajanı yeteneğine göre bulmasını sağlayacak.
4. Birden fazla ajanın katıldığı bir konuşma odası açacak.
5. Mesajları, görevleri ve çalıştırma olaylarını zaman çizelgesinde izleyecek.

Bu maddeler **hedeflenen özelliklerdir**; mevcut bir uygulamanın özellik listesi değildir.

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

Bu örnek bir **kavramsal sözleşmedir**; uygulama API'si henüz tanımlanmış değildir. Model kimlikleri ve araç izinleri kuruluma göre doğrulanmalıdır. Anahtarlar ve erişim belirteçleri ajan tanımına düz metin olarak yazılmamalıdır.

Bir ajan devre dışı bırakıldığında eski konuşmaların ajan kimliği korunur. Her çalıştırma, kullanılan ajan tanımının sürümünü veya anlık görüntüsünü kaydeder; böylece geçmiş sonuçlar daha sonra açıklanabilir.

## Ajanlar nasıl konuşacak?

### Doğrudan mesaj

Bir ajan izin verilen başka bir ajana görev veya soru gönderir. Mesaj, konuşma ve varsa görev kimliğiyle saklanır.

### Yeteneğe göre keşif

Gönderen, sabit bir ajan adı yerine `postgresql` gibi bir yetenek isteyebilir. Kayıt katmanı uygun ve etkin adayları bulur; yönlendirme kuralları hedefi seçer. Birden çok aday veya hiç aday bulunmaması açıkça ele alınır.

### Devir ve koordinasyon

İleride bir ajan konuşmanın sorumluluğunu başka ajana devredebilir veya bir koordinatör birden çok ajana alt görev atayabilir. Bu akışlarda yetki, görev sahibi ve tamamlanma koşulu kayıtlı olmalıdır.

### Oda / grup konuşması

Bir konuşmaya birden fazla ajan katılabilir. Örneğin mimar, geliştirici ve güvenlik ajanı aynı konuda görüş belirtir. İlk sürümde söz hakkı sırası ve bitiş koşulu çalışma zamanı tarafından belirlenir; kontrolsüz, sonsuz ajan konuşmaları engellenir.

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
- Tur, devir, süre ve maliyet limitleri; döngü tespiti ve durdurma mekanizması bulunur.
- Teslimat ve tekrar denemeleri aynı görevin yanlışlıkla iki kez sonuç üretmesini önleyecek biçimde tasarlanır.
- Hatalar, denemeler ve kararlar olay kayıtlarında izlenir; gizli bilgiler loglara yazılmaz.
- Çalışma sırasındaki bir ajan değişikliği, başlamış çalıştırmanın sürümünü geriye dönük değiştirmez.

## İlk çalışan dilim

İlk uygulama; ajanları API üzerinden kaydeder, bir konuşmaya iki ajan ekler ve bir ajanın diğerine gönderdiği mesajı tek bir genel LangGraph akışı üzerinden çalıştırır. Model seçimi ve talimatlar kayıtlı ajan tanımından yüklenir. Bir ajana özel Python sınıfı veya ayrı derlenmiş grafik gerekmez.

Mesajlar, çalıştırmalar, ajan tanımı anlık görüntüleri ve sıralı olaylar veritabanında saklanır. İlk sağlayıcı OpenAI'dır; otomatik testler sahte sağlayıcı kullanır ve gerçek bir model çağrısı yapmaz.

### Gereksinimler

- Python 3.11 veya üstü
- [uv](https://docs.astral.sh/uv/)
- Gerçek bir model çalıştırmak için OpenAI API anahtarı

### Yerelde çalıştırma

```bash
uv sync --extra dev
cp .env.example .env
```

`.env` dosyasına `OPENAI_API_KEY` değerini ekleyin. Ajan kayıtları varsayılan olarak `data/agent_runtime.db` SQLite veritabanında tutulur. Ardından:

```bash
uv run uvicorn agent_runtime_platform.main:app --app-dir src --reload
```

API belgeleri `http://127.0.0.1:8000/docs` adresinde açılır. Kontrolleri çalıştırmak için:

```bash
uv run pytest
```

PostgreSQL kullanmak için `AGENT_RUNTIME_DATABASE_URL` değerini örneğin `postgresql+psycopg://user:password@localhost:5432/agent_runtime` olarak ayarlayın.

### API akışı

1. `POST /agents` ile iki ajan oluşturun. Her biri için `name`, `instructions`, `model_provider` (`openai`) ve `model_name` gerekir. `capabilities` isteğe bağlıdır; örneğin `{"capabilities": ["backend", "api"]}`.
2. `POST /conversations` ile `agent_ids` listesini gönderin.
3. `GET /agents?capability=backend` ile bu yeteneğe sahip etkin ajanları arayın. Eşleşme tamdır; yetenekler kaydedilirken ve aranırken boşluklardan arındırılıp küçük harfe dönüştürülür.
4. `POST /conversations/{conversation_id}/messages` isteğinde `sender_agent_id`, `content` ve alıcılardan yalnızca birini gönderin: `recipient_agent_id` veya `recipient_capability`. Yetenek eşleşmesi tek bir etkin ajan bulursa o ajan konuşmaya otomatik eklenir. Hiç eşleşme yoksa `404`, birden fazla eşleşme varsa `409` döner. Kimlikle gönderimde iki ajan da önceden konuşma üyesi olmalıdır.
5. Yanıttaki çalıştırma kimliğiyle `GET /runs/{run_id}` üzerinden mesajları, durum bilgisini ve olay izini okuyun. Konuşmanın tamamı `GET /conversations/{conversation_id}` üzerinden alınabilir.

`PATCH /agents/{agent_id}` ajan ayarlarını, yeteneklerini değiştirir veya `{"enabled": false}` ile yeni çalıştırmalarda kullanılmasını engeller. Her değişiklik ajan sürümünü artırır. Çalışan her görev, başlangıçta kullandığı talimat/model/yetenek anlık görüntüsünü saklar; çalıştırma API'si talimat içeriğini döndürmez.

İstek gövdesi ve hata biçimleri için `/docs` içindeki OpenAPI arayüzünü kullanın. Gerçek model çağrısı OpenAI API kullanımı doğurur; testler bu servise bağlanmaz.

## MVP kapsamı ve kabul ölçütleri

| İşlev | Kabul ölçütü |
| --- | --- |
| Ajan yönetimi | Yeni ajan oluşturulur; düzenlenir; devre dışı bırakılır; geçmiş mesajlar okunabilir kalır. |
| Model ve araç yapılandırması | Ajanın model ve izin verilen araçları kaydedilir; geçersiz seçimler reddedilir. |
| Keşif | Etkin ajanlar yetenekle aranır; sonuç yoksa açık hata döner. |
| Mesajlaşma | İki ajan arasında mesaj ve yanıt konuşma kaydında görünür. |
| Oda | Bir konuşmaya birden fazla ajan eklenir; sıra ve durdurma kuralı uygulanır. |
| İzlenebilirlik | Başlama, yönlendirme, araç çağrısı, hata ve bitiş olayları zaman sırasıyla görülebilir. |

İlk sürümün üzerinde çalışacağı en küçük uçtan uca senaryo: Kullanıcı iki ajan oluşturur; birine görev verir; bu ajan diğerini yeteneğine göre bulur; mesaj gönderir; yanıtı alır; kullanıcı bütün akışı ve sonucu arayüzde görür.

## Sonraki aşamalar

1. Görev devri, koordinatör akışı ve gelişmiş grup tartışmaları.
2. Kalıcı kuyruk ve dağıtık işçiler; yük arttığında Redis Streams, NATS veya benzeri bir bileşen değerlendirmesi.
3. MCP araç bağlayıcıları ve uzak ajan sistemleriyle A2A uyumluluğu.
4. Zamanlanmış görevler, bellek, onay akışları ve görsel ajan ilişkileri editörü.

## Teknoloji yönü

**Kararlar:** Ajan akışlarını çalıştırmak için LangGraph, HTTP API için FastAPI, veriye erişim için SQLAlchemy kullanılacak. Yerel kurulum SQLite ile başlar; aynı şema PostgreSQL'e de bağlanabilir. Ajan tanımları kayıt katmanında veri olarak tutulur ve ortak LangGraph akışı bunları çalıştırma anında yükler. İlk model sağlayıcısı OpenAI'dır.

Ürün katmanı ajan kataloğu, izinler, konuşmalar, mesajlaşma ve oda davranışlarından sorumlu olacak. React/Next.js arayüzü sonraki adımda değerlendirilecek. İlk aşamada ayrı mesaj kuyruğu eklenmeyecek.

Mevcut dilimler API tabanlıdır. Kullanıcı arayüzü, grup odaları, kuyrukta çalışan işler, kimlik doğrulama, MCP/A2A ve veritabanı migration yönetimi sonraki işlerin kapsamındadır.
