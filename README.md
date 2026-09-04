# DOE-SP Bridge

Micro-API para descoberta e **localização documental precisa** de publicações do Diário Oficial do Estado de São Paulo.

## Arquitetura

- **Bridge:** encontra e valida a ocorrência e devolve a coordenada documental.
- **Skill/assistente:** lê as páginas indicadas e interpreta o ato administrativo completo.

A bridge não transforma um snippet em conclusão administrativa. Quando os metadados oficiais não informam a página, ela usa o texto do PDF oficial apenas para localizar a ocorrência; a interpretação continua a cargo da skill.

## Fontes oficiais

- Busca: `https://do-api-web-search.doe.sp.gov.br/v2/advanced-search/publications`
- Metadados auxiliares: `https://do-api-web-search.doe.sp.gov.br/v2/journals`
- Página individual: `https://doe.sp.gov.br/...`
- Edição certificada: `https://do-api-publication-pdf.doe.sp.gov.br/v1/editions/{edition_id}`

Nenhuma fonte não oficial é usada pela bridge para localizar a edição ou a página.

## `documentLocator`

Cada match de `/api/me` e `/api/me/today` tenta devolver `edition_id`, URL da edição, volume, número, caderno, seção, `match_page`, faixa da publicação e páginas recomendadas para leitura integral pela skill.

Estados principais:

- `resolved`
- `edition_not_resolved`
- `edition_found_match_not_located`
- `locator_error`

## Endpoints

- `GET /api/health`
- `GET /api/search`
- `GET /api/contest/auditor-cge?from_date=...&to_date=...`
- `GET /api/me/today`
- `GET /api/me?from_date=...&to_date=...`
- `GET /api/me/log?from_date=...&to_date=...`
- `GET /api/context?slug=...`

`/api/contest/auditor-cge` pesquisa a nomenclatura oficial do cargo e o Edital CGE nº 03/2025, deduplica as publicações e descarta menções funcionais a auditores sem sinais do concurso. A resposta usa `match_count/matches`, o mesmo contrato de `/api/me`.

Quando `editionPages` não contém a página, a bridge baixa a edição oficial e procura âncoras do título e do trecho da publicação. O resultado registra a fonte da localização e se houve leitura do PDF pela bridge.

O processamento de PDFs é serializado por instância para evitar sobreposição de edições grandes. Downloads inválidos são repetidos uma vez e edições de até 64 MB são aceitas por padrão. Esses limites podem ser ajustados por `DOESP_PDF_DOWNLOAD_ATTEMPTS` e `DOESP_MAX_PDF_BYTES`.

Quando a página individual existe, mas seu texto não é encontrado no PDF certificado da edição indicada pela própria API oficial, a bridge mantém a publicação e informa que a página não foi confirmada. Ela não associa o ato a uma página apenas por coincidência de órgão, cargo ou palavras genéricas.

## Deploy contínuo

Este repositório é a fonte permanente da bridge. A branch `main` deve estar conectada ao projeto Vercel de produção. Todo commit em `main` gera um novo deployment automaticamente, mantendo o mesmo projeto/domínio.
