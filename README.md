# DOE-SP Bridge

Micro-API para descoberta e **localização documental precisa** de publicações do Diário Oficial do Estado de São Paulo.

## Arquitetura

- **Bridge:** encontra e valida a ocorrência e devolve a coordenada documental.
- **Skill/assistente:** lê as páginas indicadas e interpreta o ato administrativo completo.

A bridge não deve transformar um snippet em conclusão administrativa quando o cabeçalho/tabela não estiver materialmente disponível.

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
- `GET /api/me/today`
- `GET /api/me?from_date=...&to_date=...`
- `GET /api/me/log?from_date=...&to_date=...`
- `GET /api/context?slug=...`

## Deploy contínuo

Este repositório é a fonte permanente da bridge. A branch `main` deve estar conectada ao projeto Vercel de produção. Todo commit em `main` gera um novo deployment automaticamente, mantendo o mesmo projeto/domínio.
