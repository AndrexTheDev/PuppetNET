import { fileURLToPath } from 'node:url';
/** Same publication, privacy filters and slug rules as generateStaticParams. */
import { listPublicEntities, loadEntityDataset } from '../apps/web/lib/entity-store';
import { buildEntityJsonLd, buildEntityMetadata, siteOrigin } from '../apps/web/lib/entity-seo';
const origin = siteOrigin();
const directory = fileURLToPath(new URL('../apps/web/public/data/', import.meta.url));
const { dataset } = loadEntityDataset(directory);
process.stdout.write(JSON.stringify({
  origin, generatedAt: dataset.generatedAt, demo: dataset.demo,
  entities: listPublicEntities(directory).map(entity => ({
    slug: entity.slug, type: entity.type, updatedAt: entity.updatedAt,
    metadata: buildEntityMetadata(entity), schema: buildEntityJsonLd(entity, origin),
  })),
}));
