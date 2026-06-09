/**
 * QVF Decoder — D3.js Graph Component
 * Force-directed graph visualization for table relationships
 */
import { max, min } from 'd3-array';
import { drag } from 'd3-drag';
import { easeCubicOut } from 'd3-ease';
import { forceCenter, forceCollide, forceLink, forceManyBody, forceSimulation } from 'd3-force';
import { pointer, select } from 'd3-selection';
import { zoom, zoomIdentity } from 'd3-zoom';

const d3 = {
  drag,
  easeCubicOut,
  forceCenter,
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  max,
  min,
  pointer,
  select,
  zoom,
  zoomIdentity,
};

export class GraphComponent {
  constructor(container, options = {}) {
    this.container = container;
    this.options = {
      onNodeClick: null,
      onUploadClick: null,
      showUploadButtons: false,
      ...options,
    };
    this.svg = null;
    this.simulation = null;
    this.nodes = [];
    this.edges = [];
    this.tooltip = null;
    this.zoom = null;
    this.g = null;

    this._init();
  }

  _init() {
    this.container.innerHTML = '';
    this.container.classList.add('graph-container');

    const rect = this.container.getBoundingClientRect();
    const width = rect.width || 800;
    const height = rect.height || 600;

    this.svg = d3.select(this.container)
      .append('svg')
      .attr('width', '100%')
      .attr('height', '100%')
      .attr('viewBox', `0 0 ${width} ${height}`);

    // Defs for arrow markers
    const defs = this.svg.append('defs');
    defs.append('marker')
      .attr('id', 'arrowhead')
      .attr('viewBox', '0 -5 10 10')
      .attr('refX', 20)
      .attr('refY', 0)
      .attr('markerWidth', 8)
      .attr('markerHeight', 8)
      .attr('orient', 'auto')
      .append('path')
      .attr('d', 'M0,-5L10,0L0,5')
      .attr('fill', '#475569');

    // Glow filter
    const filter = defs.append('filter')
      .attr('id', 'glow')
      .attr('x', '-50%').attr('y', '-50%')
      .attr('width', '200%').attr('height', '200%');
    filter.append('feGaussianBlur')
      .attr('stdDeviation', '3')
      .attr('result', 'blur');
    filter.append('feMerge')
      .selectAll('feMergeNode')
      .data(['blur', 'SourceGraphic'])
      .enter().append('feMergeNode')
      .attr('in', d => d);

    this.g = this.svg.append('g');

    // Zoom
    this.zoom = d3.zoom()
      .scaleExtent([0.3, 3])
      .on('zoom', (event) => {
        this.g.attr('transform', event.transform);
      });

    this.svg.call(this.zoom);

    // Tooltip
    this.tooltip = d3.select(this.container)
      .append('div')
      .attr('class', 'tooltip')
      .style('opacity', 0)
      .style('display', 'none');
  }

  update(graphData) {
    if (!graphData) return;

    this.nodes = (graphData.nodes || []).map(n => ({ ...n }));
    this.edges = (graphData.edges || []).map(e => ({ ...e }));

    const rect = this.container.getBoundingClientRect();
    const width = rect.width || 800;
    const height = rect.height || 600;

    this.svg.attr('viewBox', `0 0 ${width} ${height}`);

    // Clear
    this.g.selectAll('*').remove();

    if (this.nodes.length === 0) return;

    // Resolve edge source/target to node objects
    const nodeMap = new Map(this.nodes.map(n => [n.id, n]));
    this.edges = this.edges
      .map(e => ({
        ...e,
        source: nodeMap.get(e.source) || e.source,
        target: nodeMap.get(e.target) || e.target,
      }))
      .filter(e => typeof e.source !== 'string' && typeof e.target !== 'string');

    // Simulation
    this.simulation = d3.forceSimulation(this.nodes)
      .force('link', d3.forceLink(this.edges).id(d => d.id).distance(175))
      .force('charge', d3.forceManyBody().strength(-520))
      .force('center', d3.forceCenter(width / 2, height / 2))
      .force('collision', d3.forceCollide().radius(128));

    // Draw edges
    const edgeGroup = this.g.append('g').attr('class', 'edges');

    const edgePaths = edgeGroup.selectAll('line')
      .data(this.edges)
      .enter()
      .append('line')
      .attr('class', 'graph-edge')
      .attr('marker-end', 'url(#arrowhead)')
      .style('stroke', '#334155')
      .style('stroke-width', 2);

    // Edge labels
    const edgeLabels = edgeGroup.selectAll('text')
      .data(this.edges)
      .enter()
      .append('text')
      .attr('class', 'graph-edge-label')
      .attr('text-anchor', 'middle')
      .attr('dy', -6)
      .text(d => {
        const sf = (d.sourceField || '').replace(/%/g, '');
        return sf;
      });

    // Draw nodes
    const nodeGroup = this.g.append('g').attr('class', 'nodes');

    const nodeGs = nodeGroup.selectAll('g')
      .data(this.nodes)
      .enter()
      .append('g')
      .attr('class', 'graph-node')
      .call(d3.drag()
        .on('start', (event, d) => {
          if (!event.active) this.simulation.alphaTarget(0.3).restart();
          d.fx = d.x;
          d.fy = d.y;
        })
        .on('drag', (event, d) => {
          d.fx = event.x;
          d.fy = event.y;
        })
        .on('end', (event, d) => {
          if (!event.active) this.simulation.alphaTarget(0);
          d.fx = null;
          d.fy = null;
        })
      );

    // Node rectangles
    const nodeWidth = 296;
    const nodeHeight = (d) => {
      const base = 92;
      if (d.status === 'missing') return 104;
      const fieldCount = Math.min((d.fields || []).length, 6);
      return base + fieldCount * 22;
    };

    nodeGs.each(function(d) {
      const g = d3.select(this);
      const h = nodeHeight(d);
      const isFact = d.type === 'fact';
      const isMissing = d.status === 'missing';
      const isProcessing = d.status === 'processing';

      // Background rectangle
      g.append('rect')
        .attr('x', -nodeWidth / 2)
        .attr('y', -h / 2)
        .attr('width', nodeWidth)
        .attr('height', h)
        .attr('rx', 14)
        .attr('ry', 14)
        .attr('fill', isMissing ? '#fff7ed' : isFact ? '#f1f8f4' : '#ffffff')
        .attr('stroke', isMissing ? '#d7a24c' : isFact ? '#2f7d5b' : '#d5e1d2')
        .attr('stroke-width', isMissing ? 1.75 : 1.25)
        .attr('stroke-dasharray', isMissing ? '6,3' : 'none');

      // Header bar
      g.append('rect')
        .attr('x', -nodeWidth / 2)
        .attr('y', -h / 2)
        .attr('width', nodeWidth)
        .attr('height', 38)
        .attr('rx', 14)
        .attr('ry', 14)
        .attr('fill', isMissing ? '#f7e6bf' : isFact ? '#dcefe4' : '#e3f0f8');

      // Clip bottom corners of header
      g.append('rect')
        .attr('x', -nodeWidth / 2)
        .attr('y', -h / 2 + 26)
        .attr('width', nodeWidth)
        .attr('height', 12)
        .attr('fill', isMissing ? '#f7e6bf' : isFact ? '#dcefe4' : '#e3f0f8');

      // Status icon
      const statusIcon = isMissing ? '❌' : isProcessing ? '⏳' : '✅';
      g.append('text')
        .attr('x', -nodeWidth / 2 + 10)
        .attr('y', -h / 2 + 24)
        .attr('font-size', '11px')
        .text(statusIcon);

      // Table name
      const titleBox = g.append('foreignObject')
        .attr('x', -nodeWidth / 2 + 28)
        .attr('y', -h / 2 + 6)
        .attr('width', nodeWidth - 98)
        .attr('height', 28);

      titleBox.append('xhtml:div')
        .style('width', '100%')
        .style('height', '100%')
        .style('display', '-webkit-box')
        .style('-webkit-line-clamp', '2')
        .style('-webkit-box-orient', 'vertical')
        .style('overflow', 'hidden')
        .style('font-family', 'var(--font-sans)')
        .style('font-size', '13px')
        .style('font-weight', '700')
        .style('line-height', '1.1')
        .style('color', 'var(--text-primary)')
        .style('padding-right', '6px')
        .text(d.name || '');

      // Type badge
      if (!isMissing) {
        const typeLabel = isFact ? 'FACT' : 'DIM';
        const tx = nodeWidth / 2 - 10;
        g.append('text')
          .attr('x', tx)
          .attr('y', -h / 2 + 22)
          .attr('text-anchor', 'end')
          .attr('font-size', '9px')
          .attr('font-weight', '700')
          .attr('fill', isFact ? '#2f7d5b' : '#4f8fbf')
          .text(typeLabel);
      }

      if (isMissing) {
        // Upload button inside node
        // Use foreignObject to embed a real HTML button for better browser compatibility
        const foreignObject = g.append('foreignObject')
          .attr('x', -60)
          .attr('y', -h/2 + 40)
          .attr('width', 120)
          .attr('height', 30);

        const btnId = `upload-input-${d.id.replace(/[^a-z0-9]/gi, '-')}`;
        const label = foreignObject.append('xhtml:label')
          .attr('class', 'btn btn-primary btn-sm graph-html-btn')
          .attr('for', btnId)
          .style('width', '100%')
          .style('height', '100%')
          .style('font-size', '9px')
          .style('cursor', 'pointer')
          .html('📤 Upload File');

        const input = foreignObject.append('xhtml:input')
          .attr('id', btnId)
          .attr('type', 'file')
          .attr('accept', '.qvf')
          .style('display', 'none');

        input.on('change', (event) => {
          const file = event.target.files[0];
          if (file) {
            if (window.handleGraphUpload) {
              window.handleGraphUpload(file);
            }
          }
        });
      } else {
        const fields = (d.fields || []).slice(0, 6);
        const fieldBox = g.append('foreignObject')
          .attr('x', -nodeWidth / 2 + 12)
          .attr('y', -h / 2 + 40)
          .attr('width', nodeWidth - 24)
          .attr('height', Math.max(18, h - 58));

        const fieldWrap = fieldBox.append('xhtml:div')
          .style('width', '100%')
          .style('height', '100%')
          .style('overflow', 'hidden')
          .style('font-family', "'JetBrains Mono', monospace")
          .style('font-size', '11px')
          .style('line-height', '1.42')
          .style('color', '#334155');

        fields.forEach((field) => {
          const row = fieldWrap.append('xhtml:div')
            .style('display', 'flex')
            .style('gap', '6px')
            .style('align-items', 'flex-start')
            .style('margin-bottom', '4px');

          row.append('xhtml:span')
            .style('flex', '0 0 auto')
            .style('font-weight', '700')
            .style('color', field.isKey ? '#b45309' : '#2563eb')
            .text(field.isKey ? 'KEY' : '•');

          row.append('xhtml:span')
            .style('flex', '1 1 auto')
            .style('white-space', 'normal')
            .style('word-break', 'break-word')
            .style('overflow', 'hidden')
            .style('display', '-webkit-box')
            .style('-webkit-line-clamp', '2')
            .style('-webkit-box-orient', 'vertical')
            .text(field.name || '');
        });

        if ((d.fields || []).length > 6) {
          fieldWrap.append('xhtml:div')
            .style('margin-top', '2px')
            .style('font-size', '9px')
            .style('color', '#6f8077')
            .text(`+${d.fields.length - 6} more fields`);
        }

        // Row count
        if (d.rows) {
          g.append('text')
            .attr('x', nodeWidth / 2 - 10)
            .attr('y', h / 2 - 8)
            .attr('text-anchor', 'end')
            .attr('font-size', '9px')
            .attr('fill', '#6f8077')
            .attr('font-family', "'JetBrains Mono', monospace")
            .text(`${d.rows.toLocaleString()} rows`);
        }
      }
    });

    // Hover tooltip
    nodeGs.on('mouseover', (event, d) => {
      if (d.status === 'missing') return;
      this.tooltip
        .style('display', 'block')
        .style('opacity', 1)
        .html(`
          <div class="tooltip-title">${d.name}</div>
          <div class="tooltip-meta">${d.description || ''}</div>
          <div class="tooltip-meta" style="margin-top:4px">${(d.fields || []).length} fields · ${(d.rows || 0).toLocaleString()} rows · ${d.type}</div>
        `);
    })
    .on('mousemove', (event) => {
      const [mx, my] = d3.pointer(event, this.container);
      this.tooltip
        .style('left', (mx + 16) + 'px')
        .style('top', (my - 10) + 'px');
    })
    .on('mouseout', () => {
      this.tooltip.style('opacity', 0).style('display', 'none');
    });

    // Click handler
    nodeGs.on('click', (event, d) => {
      if (this.options.onNodeClick) {
        this.options.onNodeClick(d);
      }
    });

    // Tick
    this.simulation.on('tick', () => {
      edgePaths
        .attr('x1', d => d.source.x)
        .attr('y1', d => d.source.y)
        .attr('x2', d => d.target.x)
        .attr('y2', d => d.target.y);

      edgeLabels
        .attr('x', d => (d.source.x + d.target.x) / 2)
        .attr('y', d => (d.source.y + d.target.y) / 2);

      nodeGs.attr('transform', d => `translate(${d.x},${d.y})`);
    });

    // Center the graph once the simulation has settled
    this.simulation.on('end', () => {
      this.centerGraph();
    });

    // Fallback: also center after 2 s in case the simulation never fully
    // reaches alpha=0 (e.g. very large graphs with many nodes).
    setTimeout(() => {
      if (this.nodes.length) this.centerGraph();
    }, 2000);
  }

  centerGraph() {
    if (!this.nodes.length) return;
    const rect = this.container.getBoundingClientRect();
    const width = rect.width || 800;
    const height = rect.height || 600;

    const xs = this.nodes.map(n => n.x);
    const ys = this.nodes.map(n => n.y);
    
    const minX = d3.min(xs) - 100;
    const maxX = d3.max(xs) + 100;
    const minY = d3.min(ys) - 100;
    const maxY = d3.max(ys) + 100;
    
    const graphWidth = maxX - minX;
    const graphHeight = maxY - minY;
    
    const cx = (minX + maxX) / 2;
    const cy = (minY + maxY) / 2;

    // Calculate scale to fit all nodes
    const scale = Math.min(0.95, 0.92 / Math.max(graphWidth / width, graphHeight / height));

    const transform = d3.zoomIdentity
      .translate(width / 2, height / 2)
      .scale(scale)
      .translate(-cx, -cy);

    this.svg.transition()
      .duration(1000)
      .ease(d3.easeCubicOut)
      .call(this.zoom.transform, transform);
  }

  destroy() {
    if (this.simulation) this.simulation.stop();
    this.container.innerHTML = '';
  }
}
