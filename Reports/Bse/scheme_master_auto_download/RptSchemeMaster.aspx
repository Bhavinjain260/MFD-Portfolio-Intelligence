

<!DOCTYPE html>

<html xmlns="http://www.w3.org/1999/xhtml">

<head id="Head1"><title>
	Mutual Fund System - Bombay Stock Exchange Limited
</title>
    <script type="text/javascript" src="scripts/Validate.js"></script>
    <link href="CSS/Global.css" rel="stylesheet" type="text/css" /><link href="CSS/Jquery/jquery-ui.min.css" rel="stylesheet" type="text/css" />
    <script src="scripts/Jquery/jquery.min.js" type="text/javascript"></script>
    <script src="scripts/Jquery/jquery-ui.min.js" type="text/javascript"></script>

    <script language="javascript" type="text/javascript">
        $(window).load(function () {
            $(".PageLoader").fadeOut("slow");
        })
        function fnView() {
            $(".PageLoader").show();
        }
    </script>
</head>
<body>
    <form method="post" action="./RptSchemeMaster.aspx" id="frmOrdConfirm">
<div class="aspNetHidden">
<input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="0DlddpWp8TUcvaMCGGj7C2GikrBdOTPY/+zB6XTH1/T4siaETtj+n+2wBy26jqqys2YsMSGyL4+dAXwHJM7KjC4Wh+9gypIufkNY+lNMlbIGBufEfat4xKVQp9KQqIRhKycEDCEBIPxE3fC3u1sqL7IuW/q3xUXqUtzOAsOeOBP/A7xi050s5rsRyoZdidasd0kVfw==" />
</div>

<div class="aspNetHidden">

	<input type="hidden" name="__VIEWSTATEGENERATOR" id="__VIEWSTATEGENERATOR" value="27FA2253" />
	<input type="hidden" name="__VIEWSTATEENCRYPTED" id="__VIEWSTATEENCRYPTED" value="" />
	<input type="hidden" name="__EVENTVALIDATION" id="__EVENTVALIDATION" value="8/CPhckWSW8CgtnJZNs/4PEOqKOiKofEDSOe3UY9p/sKdPxEg54njWL82fo4amaVXB9OL2riaKN47t02UBHU5/SHJw//aNOF0kjaaQJfSy0FoK5q+E92b6nb5tRJjxk4xGpaIzhmgvjmIp+AzkNcsTDyZqgqhqkwKQDJx3BFo66rY0ibRUrE1V+ZxGbg3dlXYnus5Q==" />
</div>
        <div class="PageLoader">
        </div>
        <div align="center" style="width: 100%;">
            <table class="glbTableF" width="20%">
                <tr class="tblHeader">
                    <th>
                        <span id="lblHeader">Scheme Master Report</span> 
                    </th>
                </tr>
                <tr class="tblERow">
                    <td align="center">
                        <select name="ddlTypeOption" id="ddlTypeOption" class="glbDdl" style="width:200px;">
	<option value="SCHEMEMASTER">Scheme Code Master Details</option>
	<option value="SCHEMEMASTERDEMAT">Scheme Code Master Demat</option>
	<option value="SCHEMEMASTERPHYSICAL">Scheme Code Master Physical</option>

</select>
                    </td>
                </tr>
                <tr class="tblBtnFooter">
                    <td>
                        <input type="submit" name="btnText" value="Export to Text" id="btnText" class="glbBtnN" />
                    </td>
                </tr>
            </table>
        </div>
        <br />
        <br />
        <div id="dgGrid" style="overflow: auto; height: 300px; width: 100%;" align="center">
            
        </div>
        
    </form>
</body>
</html>
