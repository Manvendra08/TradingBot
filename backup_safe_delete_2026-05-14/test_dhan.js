
const dom_string = `
<html><head><title>Options Trader</title></head>
<body>
  <div>
    <h1>NATURALGAS APR FUT Option Chain</h1>
  </div>
  <div>
    <input placeholder="Search underlying" />
  </div>
  <table>
    <thead>
      <tr>
        <th>CE</th>
        <th>Strike Price</th>
        <th>PCR</th>
        <th>PE</th>
      </tr>
      <tr>
        <th>LTP Change</th>
        <th>OI</th>
        <th>LTP</th>
        <th>OI</th>
        <th>LTP Change</th>
      </tr>
    </thead>
    <tbody>
      <tr>
        <td>0.15</td>
        <td>0.01</td>
        <td>25.25</td>
        <td>260.00</td>
        <td>4.04</td>
        <td>9.25</td>
        <td>0.05</td>
        <td>-1.45</td>
      </tr>
    </tbody>
  </table>
</body></html>
`;
const fs = require("fs");
const jsdom = require("jsdom");
const { JSDOM } = jsdom;
const dom = new JSDOM(dom_string);
global.window = dom.window;
global.document = dom.window.document;
global.NodeFilter = dom.window.NodeFilter;
// JSDOM layout stubs
dom.window.HTMLElement.prototype.getClientRects = () => [{width: 100, height: 20}];
dom.window.getComputedStyle = () => ({ display: "block", visibility: "visible" });

const readerCode = fs.readFileSync("./chrome_extension/dhan_dom_reader.js", "utf8");
eval(readerCode);

const reader = window.NSEBOT_DHAN_DOM_READER;
console.log("is page:", reader.isDhanOptionChainPage());
const payload = reader.extractDhanOptionChainPayload();
console.log(JSON.stringify(payload, null, 2));

