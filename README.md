# MixBot — Bot de Discord para Mix de CS2

Bot completo em Python para gerenciar partidas **mix** (5v5) de Counter-Strike 2 em comunidades do Discord. Cuida da fila de jogadores, ranqueamento ELO, criação automática de partidas via RCON em servidores CS2 com o plugin **MatchZy**, integração com Steam e FACEIT, detecção de smurfs, torneios, sistema de punições, VIP via Stripe e muito mais.

---

## ✨ Funcionalidades

### 🎮 Sistema de Mix / Fila
- **Fila inteligente** — jogadores entram na sala de voz "Próximo" e são movidos automaticamente quando há 10 jogadores
- **Aceitar / Recusar mix** — convoca os 10 com botões e timeout
- **Votação de capitães** — cada jogador vota em quem será capitão
- **Pick de times** — capitães alternam escolhendo jogadores
- **Veto de mapas** — capitães alternam banimentos até restar um mapa
- **Criação automática da partida** no servidor CS2 via RCON + MatchZy
- **Movimentação pós-partida**: vencedores sobem, perdedores descem, próximo da fila entra

### 📊 Ranking e Estatísticas
- **Sistema de ELO** com cálculo baseado em resultado + performance individual (ADR)
- **Perfil completo** do jogador (`/perfil`) com kills, deaths, assists, ADR, win streak, total de partidas
- **Ranking geral** (`/ranking`) — Top jogadores da comunidade
- **Histórico de partidas** (`/historico`)
- **MVP destacado** no resumo da partida (maior damage)

### 🔌 Integrações
- **Steam** — vincula conta Discord ao SteamID64, validação, busca de dados da Steam API
- **FACEIT** — integração opcional para vinculação de perfil
- **CS2 ↔ Discord Chat Bridge** — mensagens do chat do CS2 aparecem no Discord e vice-versa
- **Monitor de servidores CS2** — acompanha status dos servidores online/offline

### 🛡️ Moderação e Comunidade
- **Detecção de smurfs** — análise de contas suspeitas
- **Sistema de denúncias** — tickets abertos pelos jogadores
- **Punições automáticas** — timeout/ban por recusas, abandono, comportamento
- **Mensagens de boas-vindas** para novos membros
- **Painéis fixos** — cadastro, denúncias, punições

### 💳 VIP e Monetização
- **VIP via Stripe** — planos pagos com benefícios no servidor
- **Pagamentos processados pelo próprio bot** com integração Stripe

### 🏆 Torneios
- **Sistema de torneios** — chaveamento, partidas programadas, gerenciamento via Discord

---

## 📋 Pré-requisitos

Antes de começar, você precisará ter/configurar:

- **Python 3.10 ou superior** instalado no sistema
- **MySQL 8** (local ou remoto) — o bot usa `aiomysql` com connection pooling
- **Um servidor CS2** rodando o plugin [**MatchZy**](https://github.com/shobhitp/MatchZy) com RCON habilitado (este é um **pré-requisito obrigatório** — o tutorial não cobre a instalação do servidor CS2)
- **Uma conta no [Discord Developer Portal](https://discord.com/developers/applications)** para criar o bot e obter o token
- **Chaves de API**:
  - [Steam Web API Key](https://steamcommunity.com/dev/apikey) — obrigatória
  - FACEIT API Key — opcional
- **Git** instalado

---

## 🚀 Instalação — do zero

### Passo 1: Clonar o repositório

```bash
git clone https://github.com/brunao-souza/mixbot-cs2.git
cd mixbot-backend
```

### Passo 2: Criar e ativar um ambiente virtual

```bash
# Linux / macOS
python3 -m venv venv
source venv/bin/activate

# Windows
python -m venv venv
venv\Scripts\activate
```

### Passo 3: Instalar as dependências

```bash
pip install -r requirements.txt
```

### Passo 4: Criar o bot no Discord Developer Portal

1. Acesse [https://discord.com/developers/applications](https://discord.com/developers/applications)
2. Clique em **New Application** e dê um nome para o seu bot
3. Vá na aba **Bot** e clique em **Add Bot**
4. Copie o **TOKEN** gerado — você vai colocar no `.env`
5. Na mesma aba, ative as **Privileged Gateway Intents**:
   - `Server Members Intent`
   - `Message Content Intent`
6. Vá em **OAuth2 > URL Generator**:
   - Marque os scopes: `bot` e `applications.commands`
   - Marque a permissão **Administrator** (ou selecione as permissões mínimas necessárias)
   - Copie a URL gerada e abra no navegador para convidar o bot para o seu servidor Discord

### Passo 5: Configurar o banco de dados MySQL

Conecte ao MySQL e execute:

```bash
mysql -u root -p
```

```sql
CREATE DATABASE bot CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'mixbot'@'localhost' IDENTIFIED BY 'sua_senha_aqui';
GRANT ALL PRIVILEGES ON bot.* TO 'mixbot'@'localhost';
FLUSH PRIVILEGES;
EXIT;
```

> **Nota:** As tabelas do banco são **criadas automaticamente** na primeira execução do bot — não é necessário rodar nenhum script SQL manualmente.

### Passo 6: Configurar o arquivo `.env`

```bash
cp .env.example .env
```

Abra o arquivo `.env` em um editor e preencha todos os valores. As variáveis **mínimas** para o bot funcionar são:

| Variável | O que colocar |
|---|---|
| `DISCORD_BOT_TOKEN` | Token do bot (Passo 4) |
| `DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` | Credenciais do MySQL (Passo 5) |
| `STEAM_API_KEY` | Sua chave da [Steam Web API](https://steamcommunity.com/dev/apikey) |
| `RCON_HOST` / `RCON_PORT` / `RCON_PASSWORD` | IP, porta e senha RCON do servidor CS2 |
| `CANAL_*_ID` e `*_ROLE_ID` | IDs dos canais e cargos do seu servidor Discord |

> **Como obter os IDs do Discord:** Ative o **Modo Desenvolvedor** (Configurações > Avançado > Modo Desenvolvedor), clique com o botão direito em canais, cargos ou usuários e selecione **Copiar ID**.

O arquivo `.env.example` contém a lista completa com comentários explicativos — consulte-o para detalhes de cada variável.

### Passo 7: Rodar o bot

```bash
python main.py
```

Na inicialização, o bot:
1. Inicia um **servidor web** leve (aiohttp) na porta definida em `PORT` (padrão: `10000`) — usado para health check em plataformas como Render
2. Conecta ao banco MySQL e cria as tabelas automaticamente
3. Conecta ao Discord e sincroniza os comandos slash

Para verificar se está tudo certo, abra `http://localhost:10000/health` no navegador — deve retornar `Bot is running correctly!`.

---

## 🎮 Configurando o servidor CS2 (pré-requisito)

O bot depende de um servidor **Counter-Strike 2** com os seguintes requisitos:

1. **Plugin [MatchZy](https://github.com/shobhitp/MatchZy) instalado** — é ele quem gerencia as partidas, estatísticas e webhooks
2. **RCON habilitado** — o bot usa RCON para se comunicar com o servidor
3. **Porta RCON** — geralmente a mesma porta do servidor (ex.: 26849) ou uma específica
4. **Porta GOTV** — para transmissão dos jogos
5. **`MATCHZY_WEBHOOK_KEY`** configurada — deve ser a mesma no servidor CS2 e no arquivo `.env` do bot (o MatchZy envia eventos para o bot via HTTP)

Consulte a [documentação oficial do MatchZy](https://github.com/shobhitp/MatchZy) para instruções detalhadas de instalação e configuração.

> ⚠️ O bot **não gerencia** a instalação ou manutenção do servidor CS2. Você precisa de um servidor rodando com o MatchZy antes de usar o bot.

---

## 📜 Comandos

O bot utiliza **comandos slash** (`/comando`). Abaixo os principais agrupados por categoria:

### 👑 Administração
| Comando | Descrição |
|---|---|
| `/admin` | Painel administrativo (limpar fila, resetar, etc.) |
| `/config` | Visualizar/alterar configurações do servidor |

### 🎮 Fila e Mix
| Comando | Descrição |
|---|---|
| `/fila` | Mostra a fila atual de jogadores |
| `/startmix` | Inicia o mix manualmente (se houver 10 jogadores) |
| `/perfil` | Seu perfil com estatísticas completas |

### 📊 Ranking e Estatísticas
| Comando | Descrição |
|---|---|
| `/ranking` | Top jogadores do ranking ELO |
| `/perfil [@jogador]` | Estatísticas detalhadas de um jogador |
| `/historico [@jogador]` | Últimas partidas do jogador |

### 🔗 Steam / Cadastro
| Comando | Descrição |
|---|---|
| `/cadastro` | Abre o modal para vincular SteamID e nickname |
| `/steam` | Comandos relacionados à Steam |

### ⚠️ Denúncias e Punições
| Comando | Descrição |
|---|---|
| `/denunciar` | Inicia uma denúncia contra um jogador |
| `/punicoes` | Painel de punições |

### 🏆 Torneio
| Comando | Descrição |
|---|---|
| `/torneio` | Comandos para gerenciar torneios |

### 💳 VIP
| Comando | Descrição |
|---|---|
| `/vip` | Comandos do sistema VIP |

### ℹ️ Outros
| Comando | Descrição |
|---|---|
| `/ping` | Latência do bot |
| `/ajuda` | Guia rápido de como jogar |
| `/grupos` | Links dos grupos da comunidade |

> Para a lista completa e atualizada de comandos, consulte os arquivos em `bot/cogs/`.

---

## 🚢 Deploy

### Local / VPS

Para rodar em uma VPS ou servidor dedicado:

```bash
python main.py
```

Para manter o bot rodando em segundo plano, você pode usar:

- **systemd** (Linux) — exemplo de unit:

```ini
[Unit]
Description=MixBot Discord
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/mixbot-backend
ExecStart=/home/ubuntu/mixbot-backend/venv/bin/python main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

- **tmux** ou **screen** — soluções mais simples para manter o processo ativo
- **pm2** — gerenciador de processos Node.js (pode rodar processos Python via `pm2 start python -- main.py`)

### Plataformas cloud (Render, Railway, etc.)

O bot já inclui um **servidor web de health check** (aiohttp) na porta configurada via `PORT` (padrão: 10000). Para fazer deploy:

1. Crie um **Web Service** na plataforma
2. Comando de inicialização: `python main.py`
3. Defina **todas as variáveis de ambiente** (baseadas no `.env.example`) no painel da plataforma
4. A plataforma fará ping no endpoint `/health` para manter o serviço ativo

---

## 🐛 Troubleshooting

### Bot não conecta ao Discord
- Verifique se `DISCORD_BOT_TOKEN` está correto no `.env`
- Confirme que o bot foi convidado para o servidor com as intents corretas (Server Members, Message Content)

### Erro de conexão com MySQL
- Confirme que o MySQL está rodando e acessível
- Verifique `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD` no `.env`
- Teste a conexão manualmente: `mysql -h host -u user -p`

### Comandos slash não aparecem no Discord
- Pode levar alguns minutos para sincronizar após a primeira execução
- O bot sincroniza os comandos por servidor (guild) automaticamente ao iniciar
- Se não aparecerem, tente reiniciar o bot ou kickar/convidar novamente

### Erro de RCON com o servidor CS2
- Verifique se `RCON_HOST`, `RCON_PORT` e `RCON_PASSWORD` estão corretos
- Confirme que o servidor CS2 está online e o RCON está habilitado
- Teste a conexão RCON manualmente com uma ferramenta como [rcon-cli](https://github.com/gorcon/rcon-cli)

### Bot não fala nos canais
- Verifique as permissões do bot no servidor Discord
- Confirme que os IDs dos canais (`CANAL_*_ID`) estão corretos
- O bot precisa de permissão para **Ver Canal**, **Enviar Mensagens** e **Inserir Links**

---

## 🤝 Contribuindo

Contribuições são bem-vindas! O projeto é mantido em Português.

1. Faça um **fork** do repositório
2. Crie uma branch: `git checkout -b feature/minha-feature`
3. Faça suas alterações e commit: `git commit -m 'Adiciona minha feature'`
4. Envie para o GitHub: `git push origin feature/minha-feature`
5. Abra um **Pull Request**

---

## 📄 Licença

Este projeto é distribuído como código aberto. Veja o arquivo `LICENSE` para mais informações (recomendação: licença MIT).

---

## ⚠️ Aviso

Este bot foi extraído de um ambiente de produção e genericizado para publicação. Algumas funcionalidades podem exigir configuração adicional:

- **VIP via Stripe** — requer conta Stripe e configuração de webhooks
- **Torneios** — requer configuração de chaveamento manual
- **FTP Sync** — não incluso no repositório público
- **Integração com plataformas cloud** — o health check está presente, mas cada plataforma tem suas particularidades

O **núcleo do sistema** (fila, mix, ranking, RCON, estatísticas) funciona com apenas **Discord + MySQL + um servidor CS2 com MatchZy**.
