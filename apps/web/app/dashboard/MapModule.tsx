'use client';
import palette from '../../src/styles/palette.json';
import { useEffect, useState } from 'react';
import { MapContainer, TileLayer, CircleMarker, Tooltip, useMap } from 'react-leaflet';
import type { MapPoint } from './graph-data';
import { useLanguage, useT } from './i18n';
import { NUMBER_LOCALE } from '../../lib/i18n-strings';
import 'leaflet/dist/leaflet.css';
function ResizeMap() { const map = useMap(); useEffect(() => { map.invalidateSize(); }, [map]); return null; }
export default function MapModule({ points, onSelect }: { points: MapPoint[]; onSelect: (id: string) => void }) {
  const [enabled, setEnabled] = useState(false), [tileError, setTileError] = useState(false);
  const { lang } = useLanguage(); const t = useT();
  return <div className="map-stage">
    {!enabled ? <div className="map-consent"><span className="eyebrow">{t('map.eyebrow')}</span><h2>{t('map.title')}</h2>
      <p>{t('map.consent')}</p>
      <button className="primary-button" onClick={() => setEnabled(true)}>{t('map.load')}</button>
      <p className="muted">{t('map.note')}</p>
    </div> : <>
      <MapContainer center={[45, 8]} zoom={4} minZoom={2} maxZoom={18} preferCanvas className="leaflet-host" attributionControl>
        <TileLayer url="https://tile.openstreetmap.org/{z}/{x}/{y}.png" attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors' maxZoom={19} eventHandlers={{ tileerror: () => setTileError(true) }} />
        <ResizeMap />
        {points.map(point => <CircleMarker key={point.id} center={[point.lat, point.lon]} radius={Math.min(20, 5 + Math.log2(point.count + 1))} pathOptions={{ color: palette.green, fillColor: palette.green, fillOpacity: .6, weight: 1 }} eventHandlers={{ click: () => onSelect(point.exampleId) }}>
          <Tooltip>{point.label}{point.count > 1 ? t('map.clickHint') : ''}</Tooltip>
        </CircleMarker>)}
      </MapContainer>
      {tileError && <div className="map-warning" role="status">{t('map.tileWarning')}</div>}
      <div className="map-note">{t('map.markerGroups', { count: points.length.toLocaleString(NUMBER_LOCALE[lang]) })}</div>
    </>}
  </div>;
}
