'use client';
import palette from '../../src/styles/palette.json';
import { useEffect, useState } from 'react';
import { MapContainer, TileLayer, CircleMarker, Tooltip, useMap } from 'react-leaflet';
import type { MapPoint } from './graph-data';
import 'leaflet/dist/leaflet.css';
function ResizeMap() { const map = useMap(); useEffect(() => { map.invalidateSize(); }, [map]); return null; }
export default function MapModule({ points, onSelect }: { points: MapPoint[]; onSelect: (id: string) => void }) {
  const [enabled, setEnabled] = useState(false), [tileError, setTileError] = useState(false);
  return <div className="map-stage">
    {!enabled ? <div className="map-consent"><span className="eyebrow">GEOGRAPHISCHER KONTEXT</span><h2>Orte, nicht Vermutungen.</h2>
      <p>Nur explizite Koordinaten werden angezeigt. Kartenkacheln werden direkt von OpenStreetMap geladen; dabei wird deine IP-Adresse an den Dienst übermittelt.</p>
      <button className="primary-button" onClick={() => setEnabled(true)}>OpenStreetMap-Karte laden</button>
      <p className="muted">Kein API-Key. Keine Offline-Downloads oder Vorab-Abfragen. Öffentliche Tile-Nutzung nach OSM-Richtlinie.</p>
    </div> : <>
      <MapContainer center={[45, 8]} zoom={4} minZoom={2} maxZoom={18} preferCanvas className="leaflet-host" attributionControl>
        <TileLayer url="https://tile.openstreetmap.org/{z}/{x}/{y}.png" attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors' maxZoom={19} eventHandlers={{ tileerror: () => setTileError(true) }} />
        <ResizeMap />
        {points.map(point => <CircleMarker key={point.id} center={[point.lat, point.lon]} radius={Math.min(20, 5 + Math.log2(point.count + 1))} pathOptions={{ color: palette.green, fillColor: palette.green, fillOpacity: .6, weight: 1 }} eventHandlers={{ click: () => onSelect(point.exampleId) }}>
          <Tooltip>{point.label}{point.count > 1 ? ' · Klick zeigt ein Beispiel' : ''}</Tooltip>
        </CircleMarker>)}
      </MapContainer>
      {tileError && <div className="map-warning" role="status">Kacheldienst nicht erreichbar oder begrenzt. Koordinatenmarker bleiben lokal verfügbar.</div>}
      <div className="map-note">{points.length.toLocaleString('de-DE')} räumliche Markergruppen · © OpenStreetMap-Mitwirkende</div>
    </>}
  </div>;
}
