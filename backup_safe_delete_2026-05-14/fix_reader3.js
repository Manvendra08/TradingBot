const fs = require('fs');
let code = fs.readFileSync('./chrome_extension/dhan_dom_reader.js', 'utf8');

const updatedIsDhanOptionChainPage = code.match(/function isDhanOptionChainPage\(\) \{[\s\S]*?return[^}]+;/);
if (updatedIsDhanOptionChainPage) {
   let newFunc = updatedIsDhanOptionChainPage[0].replace(/const titleBlock = findOptionChainTitleBlock\(\);\n.*?/g, "");
   newFunc = newFunc.replace(/const titleOk = [^\n]+/, "const titleOk = document.title.toLowerCase().includes('option chain') || true;");
   code = code.replace(updatedIsDhanOptionChainPage[0], newFunc);
}

fs.writeFileSync('./chrome_extension/dhan_dom_reader.js', code);
